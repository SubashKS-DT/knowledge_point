"""DARP LAW ARC - MKP (molecule knowledge points).

Your MKP prompt, generator 1 + generator 2, then the judge, saved to the mkp table in the CLUSTER|TYPE|ELEMENT|RESULTS
format that AKP reads.

  1. The source block comes from the IKP (exact node text, pages, parents, footnotes); no PDF search.
  2. One legal unit (section / rule / schedule paragraph / circular paragraph) = one cluster C_<unit key>.
  3. Models by complexity: SIMPLE and COMPLEX tiers; every role is {"llm", "model"} in permission.json.
  4. Python is the CHECKER: it builds the reference text of every MKP from the source (mkp_ss/draft.py),
     gives the judge a PRE-GATE REPORT on both models, and rejects any MKP whose words differ from the source
     (including every shall / may / not / without / only ... word: Config/settings.json mkp.word_lists).
  5. CACHE PER PROVISION: before any model call, every MKP of the unit is looked up in the database. Only the MKPs
     that are missing, or whose source text changed, go to the models; the rest are reused as they are.
  6. VERSION ONLY WHEN DIFFERENT: a re-run saves a new mkp_version only for an MKP whose text, knowledge type or
     phrases really changed (the screen shows v1 and v2); identical answers keep the saved version.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kp_console import log, say, show_change
from mkp_ss.check import GateError, fidelity, four, judge_rows, parse_candidate, pregate, standard_gate
from mkp_ss.draft import Drafter
from mkp_ss.excel import append_excel
from mkp_ss.scope import Batch, ask_scope, batches_for, complexity, label_of, resolve_token, selectable_children
from mkp_ss.source import judge_prompt, mkp_prompt, unit_context
from mkp_ss.store import MkpStore, sha

BASE_DIR = Path(__file__).resolve().parent
ROLES = ("generator_1", "generator_2", "judge")


# ---------------------------------------------------------------- settings / paths

def validate_mkp_settings(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("mkp settings must be a JSON object")
    s = dict(raw)
    for key, default in (("enabled", False), ("ask_user_input", True), ("use_cache", True), ("save_to_db", True),
                         ("export_excel", True), ("verify_blocks", True)):
        v = s.get(key, default)
        if type(v) is not bool:
            raise ValueError(f"mkp.{key} must be true or false")
        s[key] = v
    if not s["save_to_db"]:
        raise ValueError("mkp.save_to_db must be true (AKP reads the MKPs from the database)")
    s["max_retries"] = int(s.get("max_retries", 3))
    s["scope"] = s.get("scope", "FULL")
    s["excel_file"] = str(s.get("excel_file", "mkp.xlsx"))
    s["schema"] = str(s.get("schema", "public"))
    s["table"] = str(s.get("table", "mkp"))
    s["complex_threshold"] = float(s.get("complex_threshold", 20))
    s["judge_retries"] = int(s.get("judge_retries", 1))
    s["registry_limit"] = int(s.get("registry_limit", 12))
    s["stop_after_batches"] = int(s.get("stop_after_batches", 0) or 0)
    s["excel_every"] = max(1, int(s.get("excel_every", 10)))
    s["fidelity_check"] = str(s.get("fidelity_check", "BLOCK")).upper()
    if s["fidelity_check"] not in ("BLOCK", "WARN"):
        raise ValueError("mkp.fidelity_check must be BLOCK or WARN")
    s["word_lists"] = s.get("word_lists") or {}
    models = s.get("models") or {}
    for tier in ("SIMPLE", "COMPLEX"):
        for role in ROLES:
            r = (models.get(tier) or {}).get(role)
            if not isinstance(r, dict) or not r.get("llm") or not r.get("model"):
                raise ValueError(f"permission.json llm.mkp.{tier}.{role} needs llm and model")
    s["models"] = models
    return s


def build_paths(permission: Optional[dict] = None) -> dict:
    from kp_settings import build_paths as _paths
    return _paths(permission)


def document_version(pdf_path: Path):
    digest = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    return "V_" + digest[:16].upper(), digest          # AKP reads this version_id


def record_failure(paths: dict, doc: str, key: str, stage: str, error) -> None:
    out = Path(paths["output_dir"])
    ids = out / "mkp_failed_provisions.txt"
    previous = set(ids.read_text(encoding="utf-8").splitlines()) if ids.exists() else set()
    if key not in previous:
        with ids.open("a", encoding="utf-8") as fh:
            fh.write(key + "\n")
    msg = " ".join(str(error).split())[:1200]
    with (out / "mkp_failed_details.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} | {doc} | {key} | {stage} | "
                 f"{type(error).__name__}: {msg}\n")
    say(f"  FAILED : {key} ({stage}) {msg[:200]}")


def _who(role: dict) -> str:
    return f"{role['llm'].title()} {role['model']}"


# ---------------------------------------------------------------- model calls

class Models:
    """GEMINI / CLAUDE through llm_runtime.py with the model chosen per role; raw answers cached in the DB."""

    def __init__(self, paths: dict, store: MkpStore, use_cache: bool, fake: Optional[Callable] = None):
        self.paths, self.store, self.use_cache, self.fake = paths, store, use_cache, fake
        self.runtimes: Dict[str, dict] = {}
        self.calls = 0

    def call(self, provider: str, model: str, prompt: str) -> str:
        key = sha(provider, model, prompt)
        if self.use_cache:                        # the same prompt was answered before: never pay twice
            hit = self.store.llm_get(key)
            if hit is not None:
                return hit
        if self.fake is not None:
            raw = self.fake(provider, model, prompt)
        else:
            from llm_runtime import call_llm, create_llm_runtime
            if provider not in self.runtimes:
                self.runtimes[provider] = create_llm_runtime(provider, self.paths)
            runtime = dict(self.runtimes[provider])
            runtime["model"] = model
            raw, _ = call_llm(runtime, prompt)
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(f"{provider} {model} returned an empty answer")
        self.calls += 1
        self.store.llm_put(key, f"{provider}:{model}", raw)
        return raw

    def call_role(self, role: dict, prompt: str) -> str:
        try:
            return self.call(role["llm"], role["model"], prompt)
        except Exception as first:
            log(f"MODEL ERROR : {_who(role)}: {type(first).__name__}: {str(first)[:150]} - retrying once")
            return self.call(role["llm"], role["model"], prompt)

    def pair(self, role_1: dict, role_2: dict, prompt: str):
        def run(role):
            try:
                return self.call_role(role, prompt), None
            except Exception as exc:
                return "", exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(run, role_1), pool.submit(run, role_2)
            return a.result(), b.result()


def make_embedder():
    """MKP embedding through vector_store.py (permission.json llm.embedding)."""
    from vector_store import embed_text, load_vector_config
    config = load_vector_config()
    if not config["enabled"]:
        log("EMBEDDINGS : vector.enabled=false - MKPs saved without embeddings")
        return lambda text: None
    return lambda text: embed_text(text, "RETRIEVAL_DOCUMENT", config)


# ---------------------------------------------------------------- one document

class MkpRun:
    def __init__(self, document: dict, permission: dict, paths: dict, settings: dict, conn,
                 fake_llm: Optional[Callable] = None, embed: Optional[Callable] = None):
        import ikp_engine as ikp
        from ikp_ss.reader import IkpIndex
        from ikp_ss.store import Store as IkpStore
        self.doc, self.permission, self.paths, self.s = document, permission, paths, settings
        self.code = document["code"].upper()
        self.pdf = paths["law_dir"] / document["file"]
        if not self.pdf.is_file():
            raise FileNotFoundError(f"PDF missing: {self.pdf}")
        ikp_s = ikp.validate_ikp_settings(permission.get("ikp", {}))
        latest = IkpStore(conn, ikp_s["schema"], ikp_s["table_prefix"]).latest_version(self.code)
        if not latest:
            raise RuntimeError(f"{self.code}: no IKP saved - run IKP first")
        self.ikp_version = latest[0]
        self.version, self.doc_hash = document_version(self.pdf)
        if self.doc_hash != latest[1]:
            raise RuntimeError(f"{self.code}: the IKP was built from a different PDF - run IKP again")
        self.idx = IkpIndex.from_db(conn, self.code, ikp_s["schema"], ikp_s["table_prefix"], self.ikp_version)
        self.store = MkpStore(conn, settings["schema"], settings["table"])
        self.store.ensure()
        self.run_id = datetime.now(timezone.utc).strftime("MKP_%Y%m%dT%H%M%S") + "_" + self.code
        self.drafter = Drafter(self.idx, document["type"].upper(), document["name"])
        self.models = Models(paths, self.store, settings["use_cache"], fake_llm)
        self.embed = embed if embed is not None else make_embedder()
        self.base_prompt = paths["mkp_prompt"].read_text(encoding="utf-8")
        self.judge_base = paths["mkp_judge_prompt"].read_text(encoding="utf-8")
        self.excel = paths["output_dir"] / settings["excel_file"]
        self.pending: Dict[str, list] = {}
        self.pending_units = 0

    def _excel(self, by_provider: dict) -> None:
        """Buffer rows; the workbook is written every `excel_every` units and at the end (the DB is saved at once)."""
        if not self.s["export_excel"]:
            return
        for k in ("GEMINI", "CLAUDE", "STANDARD"):
            self.pending.setdefault(k, []).extend(by_provider.get(k) or [])
        self.pending_units += 1
        if self.pending_units >= self.s["excel_every"]:
            self.flush_excel()

    def flush_excel(self) -> None:
        if self.s["export_excel"] and self.pending_units:
            append_excel(self.excel, self.doc, self.pending)
            self.pending, self.pending_units = {}, 0

    # -- cache per provision
    def cache_state(self, drafts) -> dict:
        """Which MKPs of this unit are already saved with the same source words."""
        keys = []
        for d in drafts:
            keys.append(d.mkp_key)
            if d.kind == "HEADING":
                keys.append(f"{d.citation_key}#TEXT")     # a heading-only node is saved as #TEXT
        stored = self.store.stored_mkps(self.version, keys)
        done, missing, changed = [], [], []
        for d in drafts:
            if d.kind == "HEADING":
                rec = next((stored[k] for k in (d.mkp_key, f"{d.citation_key}#TEXT")
                            if k in stored and stored[k]["knowledge_type"] == "HEADING"), None)
            else:
                rec = stored.get(d.mkp_key)
                if rec and rec["knowledge_type"] == "HEADING":
                    rec = None
            if rec is None:
                missing.append(d)
            elif d.kind in ("TEXT", "METADATA") and fidelity(rec["text"], d.text):
                changed.append((d, rec))
            else:
                done.append(d)
        return {"done": done, "missing": missing, "changed": changed, "mkps": len(drafts)}

    def run_unit(self, batch: Batch) -> dict:
        full = unit_context(self.drafter, batch, self.doc)
        if not full["drafts"]:
            return {"status": "EMPTY", "rows": 0}
        state = self.cache_state(full["drafts"])
        todo = state["missing"] + [d for d, _ in state["changed"]]
        if not self.s["use_cache"]:
            todo, reason = list(full["drafts"]), "use_cache=false"
        elif not todo:
            return {"status": "CACHED", "rows": 0, "mkps": state["mkps"], "cached": state["mkps"], "llm_calls": 0,
                    "notes": []}
        else:
            parts = []
            if state["missing"]:
                parts.append(f"{len(state['missing'])} not in database ({', '.join(d.mkp_key for d in state['missing'][:4])}"
                             + (" ..." if len(state["missing"]) > 4 else "") + ")")
            if state["changed"]:
                parts.append(f"{len(state['changed'])} with changed source text")
            reason = "; ".join(parts)
        say(f"  MKP LLM reason : {reason}" + (f"  | reused from database: {len(state['done'])}" if state["done"] else ""))
        for d, rec in state["changed"]:
            show_change(d.mkp_key, rec["text"], d.text)
        only = None if len(todo) == len(full["drafts"]) else {d.mkp_key for d in todo}
        ctx = full if only is None else unit_context(self.drafter, batch, self.doc, only_keys=only)
        notes = []
        if self.s["verify_blocks"]:
            for p in ctx["provisions"]:
                r = self.idx.read_from_pdf(self.pdf, p["key"])
                if not r["matches_index"] and r.get("reason") != "node has no PDF block":
                    notes.append(f"PDF block check {p['key']}: {r.get('reason', 'differs')}")
        tier, score = complexity(ctx["drafts"], self.idx, self.s)
        models = self.s["models"][tier]
        cluster = ctx["cluster"]["id"]
        input_sha = sha({k: ctx[k] for k in ("cluster", "provisions", "expected")}, self.base_prompt,
                        self.judge_base, models, self.doc_hash, self.s["fidelity_check"])
        scope_key = batch.root

        calls0 = self.models.calls
        registry = self.store.registry_slice(ctx["unit_key"], self.s["registry_limit"])
        prompt = mkp_prompt(self.base_prompt, ctx, registry)
        log(f"MKP MODELS : {tier} (score {score}) - {_who(models['generator_1'])} + {_who(models['generator_2'])}")
        (g_raw, g_err), (c_raw, c_err) = self.models.pair(models["generator_1"], models["generator_2"], prompt)
        if g_err and c_err:
            raise RuntimeError(f"both models failed: {g_err}; {c_err}")
        g_rows, c_rows = parse_candidate(g_raw), parse_candidate(c_raw)
        report = pregate(g_rows, c_rows, ctx)
        g_text = g_raw if not g_err else f"(no output: generator 1 failed - {g_err})"
        c_text = c_raw if not c_err else f"(no output: generator 2 failed - {c_err})"

        errors, std, audit, warnings = None, None, None, []
        for attempt in range(1 + self.s["judge_retries"]):
            log(f"MKP JUDGE  : {_who(models['judge'])}" + (f" - retry {attempt} with {len(errors)} gate errors" if attempt else ""))
            jp = judge_prompt(self.judge_base, ctx, registry, g_text, c_text, report, errors)
            raw = self.models.call_role(models["judge"], jp)
            try:
                final, audit = judge_rows(raw)
                std, warnings = standard_gate(final, ctx, self.s["fidelity_check"],
                                              word_lists=self.s.get("word_lists"))
                break
            except GateError as exc:
                errors, std = exc.errors, None
        if std is None:
            raise GateError(errors)

        by_provider = {"GEMINI": g_rows, "CLAUDE": c_rows, "STANDARD": std, "JUDGE_AUDIT": audit}
        stats = self.store.commit(self.code, self.version, cluster, std, by_provider, scope_key, input_sha,
                                  sha(ctx["provisions"]), self.doc_hash, self.embed,
                                  run_id=self.run_id, source_version=self.ikp_version,
                                  doc_type=self.doc["type"].upper())
        self._excel(by_provider)
        for c in stats.get("changed", []):
            say(f"  NEW VERSION {c['element']} (text differs from the saved MKP)")
            show_change(c["element"], c["old"], c["new"])
        if stats.get("pending"):
            notes.append(f"{stats['pending']} MKP embeddings pending ({stats.get('embed_error', '')}) - filled on next run")
        n_mkp = sum(1 for r in std if r["TYPE"] == "MKP")
        return {"status": "COMPLETE", "rows": stats["rows"], "mkps": state["mkps"], "cached": len(state["done"]),
                "generated": n_mkp, "new": len(stats.get("new", [])), "changed": len(stats.get("changed", [])),
                "unchanged": len(stats.get("unchanged", [])), "tier": tier,
                "llm_calls": self.models.calls - calls0, "notes": notes + warnings}


def run_mkp_document(document: dict, permission: dict, paths: dict, settings: dict, conn,
                     scope_roots: Optional[List[str]] = None, ask: Callable[[str], str] = input,
                     fake_llm: Optional[Callable] = None, embed: Optional[Callable] = None,
                     on_scope_complete: Optional[Callable] = None) -> dict:
    """MKP for the chosen scope of one document; every unit is handed on (AKP -> GKP) as soon as it is ready.
    No menu at the end: the caller moves on to the next document."""
    run = MkpRun(document, permission, paths, settings, conn, fake_llm, embed)
    filled = run.store.backfill_embeddings(run.code, run.embed)
    if filled:
        log(f"EMBEDDINGS : {filled} missing MKP embeddings filled in")
    totals = {"units": 0, "completed": 0, "cached": 0, "failed": 0, "empty": 0, "recovered": 0, "llm_calls": 0,
              "mkps": 0, "mkps_from_cache": 0, "mkps_new": 0, "mkps_changed": 0, "units_done": []}
    if scope_roots is not None:
        roots = scope_roots
    elif settings["ask_user_input"]:
        roots = [ask_scope(run.idx, ask)]
    else:
        tokens = settings["scope"] if isinstance(settings["scope"], list) else [settings["scope"]]
        roots = [resolve_token(run.idx, t) for t in tokens]
    work = [b for r in roots for b in batches_for(run.idx, r)]
    totals["roots"] = roots
    totals["scope_label"] = ", ".join(label_of(run.idx, r) for r in roots)
    say(f"\n{document['name']} ({run.code}) - scope {totals['scope_label']} - {len(work)} unit(s)")

    def process(batch: Batch, depth: int = 0) -> None:
        totals["units"] += 1
        try:
            r = run.run_unit(batch)
        except Exception as exc:
            record_failure(paths, run.code, batch.root, "GATE" if type(exc).__name__ == "GateError" else "RUN", exc)
            children = selectable_children(run.idx, batch.root)
            if children and depth < 3:
                totals["recovered"] += 1
                say(f"  MKP RECOVERY : {batch.root} split into {len(children)} parts")
                for c in children:
                    say(f"  - part {label_of(run.idx, c)}  ({c})")
                    process(Batch(c, label_of(run.idx, c), c, True, False), depth + 1)
            else:
                totals["failed"] += 1
            return
        totals["llm_calls"] += r.get("llm_calls", 0)
        if r["status"] == "EMPTY":
            totals["empty"] += 1
            say("  MKP : nothing to frame (heading / omitted text only)")
            return
        totals["mkps"] += r.get("mkps", 0)
        totals["mkps_from_cache"] += r.get("cached", 0)
        totals["mkps_new"] += r.get("new", 0)
        totals["mkps_changed"] += r.get("changed", 0)
        if r["status"] == "CACHED":
            totals["cached"] += 1
            say(f"  MKP : {r['mkps']} MKP(s) - all from database (no LLM)")
        else:
            totals["completed"] += 1
            say(f"  MKP : {r['mkps']} MKP(s) - {r.get('cached', 0)} from database, {r.get('generated', 0)} by LLM "
                f"({r.get('new', 0)} new, {r.get('changed', 0)} new version, {r.get('unchanged', 0)} same as saved) "
                f"- LLM calls {r.get('llm_calls', 0)}")
            with (Path(paths["output_dir"]) / "mkp_completed_provisions.txt").open("a", encoding="utf-8") as fh:
                fh.write(batch.root + "\n")
        for n in r.get("notes", []):
            log(f"NOTE : {n}")
        totals["units_done"].append(batch.root)
        if on_scope_complete is not None:
            try:
                on_scope_complete({"document": run.code, "scope": batch.label, "scope_key": batch.root,
                                   "pdf_path": str(run.pdf), "standard_rows": r["rows"],
                                   "excel": str(run.excel), "mkp_status": r["status"], "mkps": r.get("mkps", 0)})
            except Exception as exc:          # the MKPs are saved; only the hand-off failed
                record_failure(paths, run.code, batch.root, "AKP_OR_GKP", exc)

    try:
        for i, batch in enumerate(work, 1):
            say(f"\n[{i}/{len(work)}] {batch.label}  ({batch.root})")
            process(batch)
            if settings["stop_after_batches"] and i >= settings["stop_after_batches"]:
                say("STOPPED : stop_after_batches reached (test switch) - run again to resume")
                break
    finally:
        run.flush_excel()
    return totals


def main_mkp(on_scope_complete: Optional[Callable] = None, on_document_complete: Optional[Callable] = None) -> List[dict]:
    import kp_settings
    permission = kp_settings.load()
    settings = validate_mkp_settings(permission.get("mkp", {}))
    if not settings["enabled"]:
        say("MKP : switched off in permission.json")
        return []
    paths = build_paths(permission)
    for name in ("mkp_failed_provisions.txt", "mkp_completed_provisions.txt"):
        (paths["output_dir"] / name).write_text("", encoding="utf-8")
    documents = kp_settings.selected_documents(permission)
    if not documents:
        raise RuntimeError("No PDF selected: put PDFs in Law/Acts, Law/Rules, Law/Notifications or Law/Circulars and set "
                           "that kind to true under \"read\" in permission.json")
    import ikp_engine as ikp
    conn = ikp.get_connection(paths)
    results = []
    try:
        for document in documents:
            try:
                t = run_mkp_document(document, permission, paths, settings, conn, on_scope_complete=on_scope_complete)
            except Exception as exc:
                record_failure(paths, document["code"], document["code"], "LOAD_IKP", exc)
                continue
            results.append({"document": document["code"], "name": document["name"], **t})
            if on_document_complete is not None:
                on_document_complete(results[-1])
    finally:
        conn.close()
    return results


if __name__ == "__main__":
    main_mkp()
