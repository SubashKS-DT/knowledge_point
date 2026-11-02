"""DARP LAW ARC / GKP: approved AKP atoms -> GST groups / subgroups.

Flow per MKP (latest approved AKP rows):
  A atom      candidates (concept + atom embeddings, include terms, anchor provisions, shared facts, references,
              section neighbours) -> Gemini + Claude verify in parallel -> judge only on disagreement
              -> nothing approved: wider embedding search (TG01-TG17) -> TG18 fallback -> TG18 proposal (human approval)
  A_REF       SAME atom: inherits the existing atom's approved groups (no LLM)
  AMENDMENT   Python: TG16 'Amendments / Substitutions / Omissions' + the groups of the provision it amends
  HEADING     skipped (no testable condition)
Saved per MKP as the next version (gkp_version, is_latest, inserted_at) with group / subgroup names and status
APPROVED / REVIEW / UNCLASSIFIED. After the document: coverage check of anchor provisions, Excel.
Uses project modules: db_insert, vector_store, llm_runtime, transport, akp_engine (_version, _in_scope).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor  # noqa: F401  (kept for parity with other passes)
from pathlib import Path
from typing import Callable, Dict, List, Optional

from gkp_ss import excel as excel_mod
from gkp_ss import taxonomy as tax
from gkp_ss.candidates import CandidateFinder
from gkp_ss.store import GkpStore, provision_prefix
from gkp_ss.verify import Verifier, parse_proposals
from kp_console import log, say
from transport import get_field

BASE_DIR = Path(__file__).resolve().parent
CLASSIFIER_VERSION = "GKP_V2_TWO_VERIFIERS"
ROLES = ("verifier_1", "verifier_2", "judge", "proposer")


def _permission() -> dict:
    from kp_settings import load
    return load()


def _path(v) -> Path:
    p = Path(v)
    return p if p.is_absolute() else BASE_DIR / p


def _hash(v) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


GKP_METHOD = "M2"     # M2: candidates also from the TSGs of similar atoms in other documents


def validate_gkp_settings(raw: dict, permission: dict) -> dict:
    s = dict(raw or {})
    for k, d in (("enabled", False), ("use_cache", True), ("export_excel", True), ("ask_user_input", False),
                 ("enable_tg18", True), ("propose_tg18_subgroups", True)):
        v = s.get(k, d)
        if type(v) is not bool:
            raise ValueError(f"gkp.{k} must be true or false")
        s[k] = v
    default_tax = "Config/gst_taxonomy.json" if _path("Config/gst_taxonomy.json").is_file() \
        else "Config/gst_taxonomy.json"
    s["taxonomy_file"] = s.get("taxonomy_file", default_tax)
    s["excel_file"] = s.get("excel_file", "gkp.xlsx")
    s["scope_key"] = str(s.get("scope_key", "")).strip()
    s["top_k_per_vector"] = max(1, min(20, int(s.get("top_k_per_vector", 5))))
    s["broad_top_k_per_vector"] = max(10, min(30, int(s.get("broad_top_k_per_vector", 20))))
    s["max_candidates"] = max(4, min(25, int(s.get("max_candidates", 12))))
    s["max_atoms_per_request"] = max(1, min(50, int(s.get("max_atoms_per_request", 20))))
    s["max_attempts"] = max(1, min(3, int(s.get("max_attempts", 2))))
    s["complex_pairs"] = int(s.get("complex_pairs", 40))
    # meaning-based candidates from atoms of other documents (0 = off)
    s["similar_atoms"] = max(0, min(20, int(s.get("similar_atoms", 8))))
    s["similar_atom_min"] = float(s.get("similar_atom_min", 0.75))
    s["akp_table"] = s.get("akp_table") or permission.get("akp", {}).get("table", "akp")
    s["mkp_table"] = s.get("mkp_table") or permission.get("akp", {}).get("mkp_table", "mkp")
    s["table_prefix"] = s.get("table_prefix", "gkp")
    models = s.get("models") or {}
    for tier in ("SIMPLE", "COMPLEX"):
        for role in ROLES:
            r = (models.get(tier) or {}).get(role)
            if not isinstance(r, dict) or not r.get("llm") or not r.get("model"):
                raise ValueError(f"permission.json llm.gkp.{tier}.{role} needs llm and model")
    s["models"] = models
    return s


def _paths(permission: dict) -> dict:
    from kp_settings import build_paths
    return build_paths(permission)


class Models:
    """llm_runtime with per-call model, raw answers cached in gkp_llm_cache."""

    def __init__(self, paths, store, use_cache, counts, fake=None):
        self.paths, self.store, self.use_cache, self.counts, self.fake = paths, store, use_cache, counts, fake
        self.runtimes = {}

    def __call__(self, provider: str, model: str, prompt: str) -> str:
        key = hashlib.sha256(f"{provider}|{model}|{prompt}".encode("utf-8")).hexdigest()
        if self.use_cache:
            hit = self.store.llm_get(key)
            if hit is not None:
                return hit
        if self.fake is not None:
            raw = self.fake(provider, model, prompt)
        else:
            from llm_runtime import call_llm, create_llm_runtime
            if provider not in self.runtimes:
                self.runtimes[provider] = create_llm_runtime(provider, self.paths)
            rt = dict(self.runtimes[provider])
            rt["model"] = model
            raw, _ = call_llm(rt, prompt)
        self.counts["llm_calls"] += 1
        self.store.llm_put(key, f"{provider}:{model}", raw)
        return raw


def _embedder(embed=None):
    if embed is not None:
        return embed, "TEST"
    from vector_store import embed_text, load_vector_config
    cfg = load_vector_config()
    if not cfg["enabled"]:
        raise RuntimeError("Set vector.enabled=true: GKP compares taxonomy embeddings with AKP atom embeddings")
    return (lambda text: embed_text(text, "RETRIEVAL_DOCUMENT", cfg)), cfg["embedding_model"]


def _row(base: dict, sub: Optional[dict], status: str, method: str, reason: str, cand: Optional[dict] = None,
         dec: Optional[dict] = None) -> dict:
    return dict(base, tg_id=sub["tg_id"] if sub else "", tg_name=sub["tg_name"] if sub else "",
                tsg_id=sub["tsg_id"] if sub else "", tsg_name=sub["name"] if sub else "",
                status=status, method=method, reason=reason,
                concept_score=(cand or {}).get("concept_score"), atom_score=(cand or {}).get("atom_score"),
                sources=(cand or {}).get("sources", []), model_1=(dec or {}).get("model_1"), model_2=(dec or {}).get("model_2"))


def run_gkp_scope(instrument, scope_key="ALL", version_id=None, fake_llm: Optional[Callable] = None,
                    embed: Optional[Callable] = None, settings_override: Optional[dict] = None):
    """Group the latest approved AKP atoms of one document / scope into the GST taxonomy."""
    permission = _permission()
    s = validate_gkp_settings({**permission.get("gkp", {}), **(settings_override or {})}, permission)
    if not s["enabled"]:
        log("GKP : SKIPPED")
        return {}
    data, subs = tax.load(_path(s["taxonomy_file"]))
    by_id = {x["tsg_id"]: x for x in subs}
    core = {g["tg_id"] for g in data["groups"] if g["tg_id"] != "TG18"}
    tv = f"{data.get('taxonomy_id', 'TAXONOMY')}:{tax.tg18_hash(subs)}"
    tvm = f"{tv}|{GKP_METHOD}"                 # saved on every row: taxonomy + how candidates were found
    doc_type = str(((permission.get("documents") or {}).get(str(instrument).upper()) or {}).get("type") or "").upper()
    subordinate = doc_type not in ("ACT", "RULES", "")
    prompt = (_path(permission["paths"]["prompt_dir"]) / permission["files"]["gkp_prompt"]).read_text(encoding="utf-8-sig")
    paths = _paths(permission)
    from dotenv import load_dotenv
    if paths["env_file"].exists():
        load_dotenv(paths["env_file"], override=False)
    from db_insert import get_connection, get_db_schema
    from akp_engine import _in_scope, _version
    embed_fn, emb_model = _embedder(embed)
    conn = get_connection()
    store = GkpStore(conn, get_db_schema(), s["akp_table"], s["table_prefix"])
    reasons: List[tuple] = []
    counts = {"mkps": 0, "atoms": 0, "unchanged": 0, "approved_mappings": 0, "agree": 0, "judge": 0, "same_inherited": 0, "amendment_rows": 0,
              "tg18_mappings": 0, "review": 0, "unclassified": 0, "tg18_proposals": 0, "cached_mkps": 0, "skipped_non_operative": 0,
              "coverage_gaps": 0, "llm_calls": 0, "taxonomy_embeddings": 0}
    call = Models(paths, store, s["use_cache"], counts, fake_llm)
    finder = CandidateFinder(store, by_id, s, f"{s['table_prefix']}_taxonomy")
    try:
        store.ensure()
        counts["taxonomy_embeddings"] = tax.sync(conn, get_db_schema(), subs, embed_fn, emb_model, f"{s['table_prefix']}_taxonomy")
        version = version_id or _version(conn, instrument, s["mkp_table"], scope_key or "ALL")
        mkps = {k: v for k, v in store.mkps(instrument, version).items()
                if _in_scope(provision_prefix(k), instrument, scope_key or "ALL")}
        if not mkps:
            raise RuntimeError(f"No approved AKP rows for {instrument}, {version}, {scope_key}")
        log(f"\nGKP / DOCUMENT     : {instrument}")
        log(f"GKP / SCOPE        : {scope_key or 'ALL'} / {len(mkps)} MKPs / {version}")
        log(f"GKP / TAXONOMY     : {s['taxonomy_file']} - {len(data['groups'])} groups / {len(subs)} subgroups "
              f"({counts['taxonomy_embeddings']} embedded now)")

        def classify(atoms: List[dict], mkp: str, mkp_id: str, context: list) -> Dict[str, List[dict]]:
            """Phases CORE -> CORE_EXPANDED -> TG18 -> TG18_PROPOSE. Returns rows per element."""
            out = {a["element"]: [] for a in atoms}
            ready = [a for a in atoms if not a.get("blocked")]
            for a in atoms:
                if a.get("blocked"):
                    out[a["element"]] = [_row(a["base"], None, "REVIEW", "NONE", a["blocked"])]
            pairs = sum(len(a["candidates"]) for a in ready)
            tier = "COMPLEX" if pairs > s["complex_pairs"] else "SIMPLE"
            verifier = Verifier(call, prompt, s["models"][tier], s["max_attempts"], log=log)
            ctx = {"document": instrument, "version": version, "source_mkp": mkp, "mkp_id": mkp_id, "context": context}

            def run_phase(trial, phase):
                for i in range(0, len(trial), s["max_atoms_per_request"]):
                    batch = trial[i:i + s["max_atoms_per_request"]]
                    payload_atoms = [{"element": a["element"], "type": a["type"], "concept": a.get("concept", ""),
                                      "family": a.get("family", ""), "results": a.get("results", ""),
                                      "local_results": a.get("local_results", ""),
                                      "candidates": [{k: c[k] for k in ("tsg_id", "tg_id", "group", "subgroup", "definition",
                                                                       "include", "exclude", "key_provisions", "concept_score",
                                                                       "atom_score", "sources")} for c in a["candidates"]]}
                                     for a in batch]
                    decisions = verifier.decide(payload_atoms, ctx, phase)
                    for a in batch:
                        for c in a["candidates"]:
                            d = decisions.get((a["element"], c["tsg_id"]))
                            if d and d["status"] in ("APPROVED", "REVIEW"):
                                out[a["element"]].append(_row(a["base"], by_id[c["tsg_id"]], d["status"], d["method"],
                                                              d["reason"] or "verdict missing; review required", c, d))
            run_phase([a for a in ready if a["candidates"]], "CORE")
            # wider embedding search inside TG01-TG17 for atoms with no approved / review membership
            empty = [a for a in ready if not out[a["element"]]]
            broad = []
            for a in empty:
                seen = {c["tsg_id"] for c in a["candidates"]}
                more = finder.find(a, core, s["broad_top_k_per_vector"], exclude=seen, full=False)
                if more:
                    broad.append(dict(a, candidates=more))
            run_phase(broad, "CORE_EXPANDED")
            if s["enable_tg18"]:
                tg18 = [dict(a, candidates=finder.find(a, {"TG18"}, 10, full=True)) for a in ready if not out[a["element"]]]
                run_phase([a for a in tg18 if a["candidates"]], "TG18_FALLBACK")
            left = [a for a in ready if not out[a["element"]]]
            proposals = []
            if left and s["enable_tg18"] and s["propose_tg18_subgroups"]:
                m = s["models"][tier]
                payload = {"mode": "TG18_PROPOSE", "document": instrument, "version": version, "source_mkp": mkp, "context": context,
                           "existing_tg18_subgroups": [{"tsg_id": x["tsg_id"], "name": x["name"], "definition": x["definition"]}
                                                      for x in subs if x["tg_id"] == "TG18"],
                           "atoms": [{"element": a["element"], "concept": a.get("concept"), "family": a.get("family"),
                                      "results": a.get("results"), "local_results": a.get("local_results")} for a in left]}
                try:
                    raw = call(m["proposer"]["llm"], m["proposer"]["model"], prompt + "\n\nINPUT JSON:\n" +
                               json.dumps(payload, ensure_ascii=False, default=str))
                    proposals = parse_proposals(raw, left)
                    store.save_proposals(instrument, version, mkp, fp, proposals)
                    counts["tg18_proposals"] += len(proposals)
                    for p in proposals:
                        log(f"TG18 PROPOSAL           : {p['proposal_id']} {p['name']} (pending your approval)")
                except Exception as exc:
                    log(f"TG18 PROPOSAL           : skipped - {type(exc).__name__}: {str(exc)[:120]}")
            pmap = {p["element"]: p for p in proposals}
            for a in left:
                p = pmap.get(a["element"])
                out[a["element"]] = [_row(a["base"], None, "UNCLASSIFIED", "NONE",
                                          f"taxonomy gap; pending TG18 proposal {p['proposal_id']} '{p['name']}'" if p
                                          else "no candidate subgroup approved by the verifiers")]
            return out

        def is_amendment(rows):
            r = next((x for x in rows if x["type"] == "MKP"), rows[0])
            return get_field({"RESULTS": r["results"]}, "knowledge_type").upper() == "AMENDMENT"
        # provisions first, amendments last: an amendment takes the groups of the provision it amends
        ordered = sorted(mkps.items(), key=lambda kv: (is_amendment(kv[1]), kv[0]))
        for mkp, raw_rows in ordered:
            mkp_row = next((r for r in raw_rows if r["type"] == "MKP"), raw_rows[0])
            kt = get_field({"RESULTS": mkp_row["results"]}, "knowledge_type").upper()
            mkp_id = mkp_row.get("mkp_id") or ""
            atom_rows = [r for r in raw_rows if r["type"] in ("A", "A_REF")]
            counts["mkps"] += 1
            if not atom_rows and kt != "AMENDMENT":
                counts["skipped_non_operative"] += 1
                continue
            context = [{"type": r["type"], "element": r["element"], "results": str(r["results"])[:3000]}
                       for r in raw_rows if r["type"] not in ("A", "A_REF")]
            # CACHE: grouped before from this same AKP version -> reuse, no model call
            akp_now = store.akp_state(instrument, version, mkp) or {}
            state = store.latest_state(instrument, version, mkp)
            same_akp = bool(state and akp_now) and (
                (state["akp_version"] is not None and state["akp_version"] == akp_now.get("akp_version"))
                or (state["akp_version"] is None and state["inserted_at"] is not None
                    and akp_now.get("inserted_at") is not None and state["inserted_at"] >= akp_now["inserted_at"]))
            saved_tv = str((state or {}).get("taxonomy_version") or "")
            # notifications / circulars grouped before meaning-based candidates existed are grouped once more
            method_ok = not subordinate or saved_tv.endswith("|" + GKP_METHOD)
            if (s["use_cache"] and same_akp and not state["review"] and method_ok
                    and (not state["unclassified"] or saved_tv.split("|", 1)[0] == tv)):
                counts["cached_mkps"] += 1
                log(f"MKP                    : {mkp} - GROUPED (CACHED, version {state['gkp_version']})")
                continue
            reason = ("use_cache=false" if not s["use_cache"] else "no GKP saved yet" if not state
                      else "REVIEW rows are checked again" if same_akp and state["review"]
                      else "grouped again with meaning-based candidates (TSGs of similar atoms in other laws)"
                      if same_akp and not method_ok
                      else "the taxonomy changed for UNCLASSIFIED atoms" if same_akp
                      else f"AKP changed (AKP v{state['akp_version'] or '?'} -> v{akp_now.get('akp_version')})")
            reasons.append((mkp, reason))
            rows: List[dict] = []
            atoms: List[dict] = []
            inherited_keys = []
            if kt == "AMENDMENT":
                base = {"instrument": instrument, "version_id": version, "source_mkp": mkp, "mkp_id": mkp_id, "akp_id": None,
                        "akp_element": mkp_row["element"], "akp_type": "MKP", "concept": "Amendment record"}
                lk = next((r for r in raw_rows if r["type"] == "LK" and "AMENDS" in (r["results"] or "")), None)
                target = get_field({"RESULTS": lk["results"]}, "target") if lk else provision_prefix(mkp)
                if "TSG16_06" in by_id:
                    rows.append(_row(base, by_id["TSG16_06"], "APPROVED", "AMENDMENT_RULE",
                                     f"amendment record of {target} (time fact)"))
                for m in store.approved_for_provision(target):
                    if m["tsg_id"] in by_id and m["tsg_id"] != "TSG16_06":
                        rows.append(_row(base, by_id[m["tsg_id"]], "APPROVED", "AMENDMENT_TARGET",
                                         f"amends {target}, which belongs to this subgroup"))
                inherited_keys.append(("amend", target, sorted(r["tsg_id"] for r in rows)))
            for r in atom_rows:
                base = {"instrument": instrument, "version_id": version, "source_mkp": mkp, "mkp_id": mkp_id,
                        "akp_id": r["akp_id"], "akp_element": r["element"], "akp_type": r["type"], "concept": r["concept"] or ""}
                counts["atoms"] += 1
                if r["type"] == "A_REF":
                    canonical = r.get("same_akp_id") or ""
                    if not canonical:                    # older AKP format: same_ref "inst::ver::mkp::element"
                        parts = get_field({"RESULTS": r["results"]}, "same_ref").split("::")
                        canonical = f"{parts[-2]}::{parts[-1]}" if len(parts) >= 2 else ""
                    got = store.approved_for_akp(canonical) if canonical else []
                    if got:
                        for m in got:
                            if m["tsg_id"] in by_id:
                                rows.append(_row(base, by_id[m["tsg_id"]], "APPROVED", "SAME_INHERITED",
                                                 f"same meaning as {canonical}"))
                        inherited_keys.append(("same", canonical, sorted(m["tsg_id"] for m in got)))
                        continue
                    ref = store.canonical_atom(canonical) if canonical else None
                    if not ref:
                        rows.append(_row(base, None, "REVIEW", "NONE", f"SAME atom {canonical or '?'} not found or not approved"))
                        continue
                    data_row = dict(r, concept=ref["concept"], results=ref["results"], concept_vector=ref["concept_vector"],
                                    atom_vector=ref["atom_vector"])
                    facts = store.atom_facts(instrument, version, ref["source_mkp"], ref["element"])
                else:
                    data_row = r
                    facts = store.atom_facts(instrument, version, mkp, r["element"])
                atom = {"element": r["element"], "type": r["type"], "concept": data_row["concept"] or "",
                        "results": data_row["results"] or "", "family": get_field({"RESULTS": data_row["results"] or ""}, "family"),
                        "local_results": r["results"] if r["type"] == "A_REF" else "", "source_mkp": mkp, "facts": facts,
                        "concept_vector": data_row.get("concept_vector"), "atom_vector": data_row.get("atom_vector"), "base": base}
                if not (atom["concept_vector"] or atom["atom_vector"]):
                    atom["blocked"] = "No AKP embeddings for this AKP yet (filled on the next AKP run)"
                    atom["candidates"] = []
                else:
                    atom["candidates"] = finder.find(atom, core, s["top_k_per_vector"])
                atoms.append(atom)
            fp = _hash({"v": CLASSIFIER_VERSION, "prompt": prompt, "models": s["models"], "inherited": inherited_keys,
                        # only stable candidate sources: fact / reference / section sources change as other MKPs
                        # get grouped and would make every run look new (use_cache=false re-checks with them)
                        "atoms": [(a["element"], a["type"], a["concept"], a["results"], a.get("blocked"),
                                   sorted((c["tsg_id"], c["definition"], tuple(c["include"]), tuple(c["exclude"]))
                                          for c in a["candidates"]
                                          if any(x.startswith(("concept", "atom", "term", "anchor")) for x in c["sources"])))
                                  for a in atoms]})
            log(f"\nMKP                    : {mkp} / {len(atoms)} atoms" + (" / amendment" if kt == "AMENDMENT" else ""))
            for a in atoms:
                srcs = "; ".join(f"{c['tsg_id']}[{', '.join(c['sources'][:3])}]" for c in a["candidates"][:5])
                log(f"  {a['element']:<20} {a['concept'][:28]:<28} {srcs[:150]}")
            if atoms:
                for el, rs in classify(atoms, mkp, mkp_id, context).items():
                    rows.extend(rs)
            for r in rows:
                r["taxonomy_version"] = tvm
                r["input_sha256"] = fp
                r["akp_version"] = akp_now.get("akp_version")
            ver = store.same_as_latest(instrument, version, mkp, rows)
            if ver is not None:                      # same memberships as saved: keep that version
                store.touch_akp_version(instrument, version, mkp, akp_now.get("akp_version"), tvm)
                counts["unchanged"] += 1
            else:
                ver = store.save_version(instrument, version, mkp, rows)
            store.mark_covered(instrument, version, mkp, rows)
            for r in rows:
                counts["approved_mappings"] += r["status"] == "APPROVED"
                counts["agree"] += r["status"] == "APPROVED" and r["method"] == "AGREE"
                counts["judge"] += r["status"] == "APPROVED" and r["method"] == "JUDGE"
                counts["same_inherited"] += r["method"] == "SAME_INHERITED"
                counts["amendment_rows"] += r["method"].startswith("AMENDMENT")
                counts["tg18_mappings"] += r["status"] == "APPROVED" and r["tg_id"] == "TG18"
                counts["review"] += r["status"] == "REVIEW"
                counts["unclassified"] += r["status"] == "UNCLASSIFIED"
            for el in dict.fromkeys(r["akp_element"] for r in rows):
                mine = [r for r in rows if r["akp_element"] == el]
                ok = [f"{r['tsg_id']} {r['tsg_name']} ({r['method']})" for r in mine if r["status"] == "APPROVED"]
                log(f"  FINAL {el:<14}: " + ("; ".join(ok) if ok else mine[0]["status"] + " - " + mine[0]["reason"][:90]))
            log(f"SAVED                  : version {ver} / {sum(r['status'] == 'APPROVED' for r in rows)} approved, "
                  f"{sum(r['status'] != 'APPROVED' for r in rows)} review / unclassified")
        gaps = [g for g in store.coverage(instrument, version, subs) if g[6] == "GAP"]
        counts["coverage_gaps"] = len(gaps)
        for g in gaps[:20]:
            log(f"COVERAGE GAP           : {g[3]} has {g[4]} atom(s), none approved in {g[2]} {by_id[g[2]]['name']}")
        if s["export_excel"]:
            p = excel_mod.write(_path(permission["paths"]["output_dir"]) / s["excel_file"], store, by_id)
            log(f"GKP EXCEL              : {p}")
        tsg = store.tsg_counts(instrument, version, list(mkps))
        counts["tsg_counts"] = tsg
        counts["source_mkps"] = list(mkps)
        llm_mkps = counts["mkps"] - counts["cached_mkps"] - counts["skipped_non_operative"]
        say(f"  GKP : {counts['mkps']} MKP(s) - {counts['cached_mkps']} from database, {llm_mkps} grouped now - "
            f"LLM calls {counts['llm_calls']}" + (f" - {counts['unclassified']} atom(s) UNCLASSIFIED" if counts["unclassified"] else "")
            + (f" - {counts['review']} to REVIEW" if counts["review"] else ""))
        for key, reason in reasons:
            say(f"        LLM for {key}: {reason}")
        if tsg:
            say("        atoms per TSG : " + ", ".join(f"{k} {v['atoms']}" for k, v in tsg.items()))
        log("GKP COUNTS " + json.dumps({k: v for k, v in counts.items() if k not in ("tsg_counts", "source_mkps")}))
        return counts
    finally:
        conn.close()


def approve_tg18(proposal_id: str, reject: bool = False, settings_override: Optional[dict] = None) -> str:
    """Your decision on a TG18 proposal: approve -> new TSG18_nn sub-group in the taxonomy file (the affected atoms are
    re-grouped on the next GKP run); reject -> marked REJECTED."""
    permission = _permission()
    s = validate_gkp_settings({**permission.get("gkp", {}), **(settings_override or {})}, permission)
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        store = GkpStore(conn, get_db_schema(), s["akp_table"], s["table_prefix"])
        store.ensure()
        p = store.proposal(proposal_id)
        if not p:
            raise KeyError(f"No TG18 proposal {proposal_id}")
        if reject:
            store.set_proposal_status(proposal_id, "REJECTED")
            return "REJECTED"
        sid = tax.add_tg18_subgroup(_path(s["taxonomy_file"]), p)
        store.set_proposal_status(proposal_id, "APPROVED", sid)
        log(f"TG18 PROPOSAL {proposal_id} APPROVED as {sid} in {s['taxonomy_file']} - run GKP again")
        return sid
    finally:
        conn.close()


def main_gkp():
    """Standalone: group existing AKPs for each enabled document."""
    permission = _permission()
    s = validate_gkp_settings(permission.get("gkp", {}), permission)
    if not s["enabled"]:
        log("GKP : SKIPPED")
        return []
    scope = s["scope_key"] or "ALL"
    if s["ask_user_input"]:
        scope = input("GKP scope (citation key / ALL): ").strip() or scope
    from kp_settings import selected_documents
    return [run_gkp_scope(d["code"], scope_key=scope) for d in selected_documents(permission)]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DARP LAW ARC GKP")
    ap.add_argument("--approve-tg18", metavar="PROPOSAL_ID")
    ap.add_argument("--reject-tg18", metavar="PROPOSAL_ID")
    args = ap.parse_args()
    if args.approve_tg18:
        approve_tg18(args.approve_tg18)
    elif args.reject_tg18:
        approve_tg18(args.reject_tg18, reject=True)
    else:
        main_gkp()
