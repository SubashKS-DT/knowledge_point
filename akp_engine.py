"""DARP LAW ARC - AKP: approved MKP -> conceptual AKPs -> checks -> comparison -> versioned storage.

Same flow as before:  Gemini AKP + Claude AKP  ->  Claude STD judge  ->  comparison (reconstructed MKP == source?)
Added:
  * Python checks between judge and comparison (akp_ss/guard.py): negation / qualifier / modal / number guard
    ("without" -> "with" is caught here) and structure gate (IDs, LE / G logic, orphans, SP coverage, facts).
    Failures go back to the judge with the exact errors (judge_repairs), then count as a FALSE attempt.
  * Comparison by a different model family (default gemini-2.5-pro) than the judge.
  * Fact registry (akp_ss/facts.py): facts reused across MKPs by one canonical id; atom -> fact edges.
  * HEADING / AMENDMENT / METADATA MKPs written by Python (no LLM); amendments become time facts.
  * Versions: a re-run saves akp_version n+1 (old rows SUPERSEDED, is_latest false); inserted_at on every row.
  * Model tiers SIMPLE / COMPLEX; Gemini + Claude in parallel; raw answers cached; embeddings PENDING-safe.
Requires the project modules: db_insert, llm_runtime, transport, vector_store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from psycopg2 import sql

from akp_ss import excel as excel_mod
from akp_ss.facts import FactRegistry, skip_rows, time_fact
from akp_ss.guard import engine_ready, legal_words, structure
from akp_ss.parse import family, field_list, parse_rows, parse_verdict, raw_rows, table_text
from akp_ss.refs import RefFinder, set_references
from akp_ss.store import AkpStore
from kp_console import log, say
from transport import get_field, mkp_bundles, tg_of

BASE_DIR = Path(__file__).resolve().parent
PYTHON_KINDS = {"HEADING", "AMENDMENT", "METADATA"}           # written by Python, no LLM
NON_OPERATIVE = PYTHON_KINDS | {"CROSS_REF", "ILLUSTRATION"}
ROLES = ("generator_1", "generator_2", "judge", "comparison", "same")


@dataclass
class AkpSession:
    reasons: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {"mkps": 0, "approved": 0, "cached": 0, "python_written": 0,
                                                  "atoms": 0, "unchanged": 0,
                                                  "failed": 0, "same_refs": 0, "legal_refs": 0, "new_facts": 0,
                                                  "fact_links": 0, "time_facts": 0, "embeddings_pending": 0,
                                                  "llm_calls": 0})
    failures: list = field(default_factory=list)


# ---------------------------------------------------------------- settings

def _permission() -> dict:
    from kp_settings import load
    return load()


def validate_akp_settings(raw: dict) -> dict:
    s = dict(raw or {})
    for key, default in (("enabled", False), ("use_cache", True), ("ask_user_input", True), ("save_to_db", True),
                         ("export_excel", True)):
        v = s.get(key, default)
        if type(v) is not bool:
            raise ValueError(f"akp.{key} must be true or false")
        s[key] = v
    if not s["save_to_db"]:
        raise ValueError("akp.save_to_db must be true")
    s["mkp_table"] = s.get("mkp_table", "mkp")
    s["table"] = s.get("table", "akp")
    s["cache_table"] = s.get("cache_table", "akp_cache")
    s["max_attempts"] = max(1, min(3, int(s.get("max_attempts", 2))))
    s["judge_repairs"] = max(0, int(s.get("judge_repairs", 1)))
    s["complex_threshold"] = float(s.get("complex_threshold", 8))
    s["excel_every"] = max(1, int(s.get("excel_every", 25)))
    s["excel_file"] = s.get("excel_file", "akp.xlsx")
    s["fact_slice"] = int(s.get("fact_slice", 30))
    s["legal_word_check"] = str(s.get("legal_word_check", "STANDARD")).upper()   # BLOCK | STANDARD | WARN
    if s["legal_word_check"] not in ("BLOCK", "STANDARD", "WARN"):
        raise ValueError("akp.legal_word_check must be BLOCK, STANDARD or WARN")
    s["same_min_similarity"] = float(s.get("same_min_similarity", 0.90))
    for k in ("section", "scope_key", "only_mkp"):
        s[k] = str(s.get(k, "")).strip()
    models = s.get("models") or {}
    for tier in ("SIMPLE", "COMPLEX"):
        for role in ROLES:
            r = (models.get(tier) or {}).get(role)
            if not isinstance(r, dict) or not r.get("llm") or not r.get("model"):
                raise ValueError(f"permission.json llm.akp.{tier}.{role} needs llm and model")
    s["models"] = models
    return s


def _paths(permission: dict) -> dict:
    from kp_settings import build_paths
    return build_paths(permission)


def _prompts(permission: dict) -> dict:
    from kp_settings import resolve
    prompt_dir = resolve(permission["paths"]["prompt_dir"])
    files = permission["files"]
    names = {"akp": files["akp_prompt"], "judge": files["akp_judge_prompt"],
             "comparison": files["akp_comparison_prompt"], "same": files["akp_same_prompt"]}
    out = {}
    for label, name in names.items():
        path = prompt_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"AKP {label} prompt missing: {path}")
        out[label] = path.read_text(encoding="utf-8-sig")
    return out


def _concept_taxonomy(permission: dict) -> str:
    """The GKP taxonomy as concept guidance only (unchanged)."""
    file = Path(permission.get("gkp", {}).get("taxonomy_file", "Config/gst_taxonomy.json"))
    if not file.is_absolute():
        file = BASE_DIR / file
    data = json.loads(file.read_text(encoding="utf-8-sig"))
    groups = data.get("groups") or []
    if len(groups) != 18:
        raise ValueError("AKP expects the same GST taxonomy as GKP (17 groups + TG18)")
    seen, compact = set(), []
    for g in groups:
        subs = []
        for sub in g.get("subgroups", []):
            key = sub["tsg_id"]
            if key in seen or tg_of(key) != g["tg_id"]:
                raise ValueError("Invalid or duplicated GST subgroup: " + key)
            seen.add(key)
            subs.append({"id": key, "name": sub["name"], "definition": sub["definition"]})
        compact.append({"id": g["tg_id"], "name": g["name"], "description": g.get("description", ""), "subgroups": subs})
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _table(name: str):
    from db_insert import get_db_schema
    return sql.SQL("{}.{}").format(sql.Identifier(get_db_schema()), sql.Identifier(name))


# ---------------------------------------------------------------- MKP source (kept: GKP imports _version)

def _in_scope(key, instrument, selection):
    if not selection or selection.upper() == "ALL":
        return True
    s = selection.strip()
    m = re.fullmatch(r"(\d+)\((\d+[A-Za-z]?)\)(?:\(([A-Za-z0-9]+)\))?", s)
    if m:
        sec, ss, cl = m.groups()
        s = f"{instrument}_S{sec}_SS{ss.upper()}" + (f"_C{cl}" if cl else "")
    if s.upper().startswith(instrument + "_"):
        return key == s or key.startswith(s + "_")
    if re.fullmatch(r"\d+[A-Za-z]?", s):
        prefix = f"{instrument}_S{s.upper()}"
        return key == prefix or key.startswith(prefix + "_")
    return key == s or key.startswith(s + "_")


def _version(conn, instrument, table, selection):
    """Latest MKP version (PDF version) that holds the selected scope."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT version_id FROM {} WHERE type='MKP' AND element LIKE %s AND version_id IS NOT NULL
            AND version_id <> 'LEGACY' AND COALESCE(is_latest, true) ORDER BY ctid DESC""").format(_table(table)),
                    (instrument + "%",))
        versions = list(dict.fromkeys(r[0] for r in cur.fetchall()))
    for version in versions:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT element FROM {} WHERE version_id=%s AND type='MKP' AND element LIKE %s "
                                "AND COALESCE(is_latest, true)").format(_table(table)), (version, instrument + "%"))
            if any(_in_scope(r[0].split("#", 1)[0], instrument, selection) for r in cur.fetchall()):
                return version
    raise RuntimeError(f"No Standard MKPs found for {instrument} and scope {selection}")


def _read_bundles(conn, instrument, table, version, selection, only_mkp):
    """Latest MKP + SP rows of the scope, children before parents (as before)."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("""SELECT cluster, type, element, results, mkp_id, COALESCE(mkp_version, 1) FROM {}
            WHERE version_id=%s AND COALESCE(is_latest, true) AND (type='BATCH' OR element LIKE %s) ORDER BY ctid""")
                    .format(_table(table)), (version, instrument + "%"))
        records = cur.fetchall()
    cards, clusters, mkp_versions = {}, {}, {}
    for cluster, kind, element, results, mkp_id, mver in records:
        row = {"CLUSTER": cluster, "TYPE": kind, "ELEMENT": element, "RESULTS": results, "MKP_ID": mkp_id}
        if kind == "BATCH":
            cards[cluster] = {"id": cluster, "name": get_field(row, "cluster_name"), "question": get_field(row, "question")}
        elif kind in ("MKP", "SP"):
            clusters.setdefault(cluster, []).append(row)
            if kind == "MKP":
                mkp_versions[element] = mver
    out = []
    for cluster, rows in clusters.items():
        for b in mkp_bundles(rows):
            if not _in_scope(b["key"].split("#", 1)[0], instrument, selection):
                continue
            if only_mkp and only_mkp not in (b["key"], b["mkp_id"]):
                continue
            b["mkp_version"] = mkp_versions.get(b["key"], 1)
            out.append((cards.get(cluster, {"id": cluster, "name": cluster, "question": ""}), b))
    if not out:
        raise RuntimeError("Selected scope has no approved Standard MKPs in MKP.")
    keys = {b["key"].split("#", 1)[0] for _, b in out}
    out.sort(key=lambda cb: (0 if get_field(cb[1]["mkp_row"], "parent") in keys else 1, cb[1]["key"]))
    return out


# ---------------------------------------------------------------- models / embeddings

class Models:
    def __init__(self, paths: dict, store: AkpStore, use_cache: bool, session: AkpSession,
                 fake: Optional[Callable] = None):
        self.paths, self.store, self.use_cache, self.session, self.fake = paths, store, use_cache, session, fake
        self.runtimes: Dict[str, dict] = {}

    def call(self, provider: str, model: str, prompt: str, retries: int = 1) -> str:
        key = hashlib.sha256(f"{provider}|{model}|{prompt}".encode("utf-8")).hexdigest()
        if self.use_cache:
            hit = self.store.llm_get(key)
            if hit is not None:
                return hit
        last = None
        for attempt in range(retries + 1):
            try:
                if self.fake is not None:
                    raw = self.fake(provider, model, prompt)
                else:
                    from llm_runtime import call_llm, create_llm_runtime
                    if provider not in self.runtimes:
                        self.runtimes[provider] = create_llm_runtime(provider, self.paths)
                    rt = dict(self.runtimes[provider])
                    rt["model"] = model
                    raw, _ = call_llm(rt, prompt)
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError(f"{provider} {model} returned an empty response")
                self.session.counts["llm_calls"] += 1
                self.store.llm_put(key, f"{provider}:{model}", raw)
                return raw
            except Exception as exc:
                last = exc
                log(f"MODEL ERROR            : {provider} {model}: {type(exc).__name__}: {str(exc)[:160]}"
                    + (" - retrying" if attempt < retries else ""))
        raise RuntimeError(f"{provider} {model} failed: {last}")

    def role(self, role: dict, prompt: str) -> str:
        return self.call(role["llm"], role["model"], prompt)

    def pair(self, models: dict, prompt: str):
        with ThreadPoolExecutor(max_workers=2) as pool:
            g = pool.submit(self.role, models["generator_1"], prompt)
            c = pool.submit(self.role, models["generator_2"], prompt)
            return g.result(), c.result()


def make_embedder() -> Callable:
    from vector_store import embed_text, load_vector_config
    cfg = load_vector_config()
    if not cfg["enabled"]:
        log("AKP / EMBEDDINGS   : vector.enabled=false - atoms saved without embeddings")
        return lambda text: None
    return lambda text: embed_text(text, "RETRIEVAL_DOCUMENT", cfg)


def _vectors(rows: List[dict], embed: Callable, session: AkpSession) -> Dict[int, tuple]:
    out, cache = {}, {}
    for i, row in enumerate(rows):
        if row["TYPE"] != "A":
            continue
        pair = []
        for text in (f"{row['CONCEPT']} - {family(row)}", get_field(row, "statement")):
            if text not in cache:
                try:
                    cache[text] = embed(text)
                except Exception as exc:
                    log(f"EMBEDDING              : pending ({type(exc).__name__}) - filled on the next run")
                    cache[text] = None
            pair.append(cache[text])
        out[i] = tuple(pair)
        if not all(pair):
            session.counts["embeddings_pending"] += 1
    return out


def complexity(text: str, sp_rows: list, knowledge_type: str, threshold: float):
    words = len(text.split())
    quals = len(re.findall(r"\b(provided that|unless|except|notwithstanding|subject to|either|neither|whichever|"
                           r"other than|without)\b", text.lower()))
    joins = len(re.findall(r"\b(and|or)\b", text.lower()))
    score = round(words / 25 + len(sp_rows) + 2 * quals + joins / 3, 1)
    tier = "COMPLEX" if score >= threshold or knowledge_type in ("COMPUTATION", "RATE", "TIMELINE") else "SIMPLE"
    return tier, score


# ---------------------------------------------------------------- SAME (existing atom reused)

def akp_id(source_mkp: str, element: str) -> str:
    """Stable id of an atom: '<citation_key#part>::<atom element>', e.g. CGSTA17_S7_SS1_Ca#TEXT::A_SALE."""
    return f"{source_mkp}::{element}"


def _apply_same(rows: List[dict], matches: List[dict], candidates: List[dict], min_similarity: float = 0.90) -> int:
    """SAME confirmed by the SAME90 check -> the new atom is not inserted again: TYPE A_REF, RESULTS empty,
    SAME_AKP_ID = the existing atom's akp_id. Python only checks the match is one of the supplied candidates."""
    bank = {(item["element"], c["akp_id"]): c for item in candidates for c in item.get("candidates", [])}
    by_element = {r["ELEMENT"]: r for r in rows if r["TYPE"] == "A"}
    count = 0
    for match in matches:
        new_el, canonical = str(match.get("element", "")).strip(), str(match.get("canonical", "")).strip()
        if str(match.get("same_verdict", "")).strip().upper() != "TRUE" or not str(match.get("reason", "")).strip():
            continue
        cand, row = bank.get((new_el, canonical)), by_element.get(new_el)
        if not cand or not row or row["TYPE"] != "A" or float(cand["similarity"]) <= min_similarity:
            continue
        row["_facts"] = field_list(row, "facts")              # the fact links stay (network), the text does not
        row["TYPE"] = "A_REF"
        row["RESULTS"] = ""
        row["SAME_AKP_ID"] = canonical
        row["SAME_REASON"] = str(match.get("reason", ""))[:300]
        count += 1
    return count


# ---------------------------------------------------------------- one scope

def run_akp_scope(pdf_path, instrument=None, section=None, scope_key=None, only_mkp=None,
                    fake_llm: Optional[Callable] = None, embed: Optional[Callable] = None, settings_override=None):
    """AKP for one scope: Gemini / Claude -> judge -> Python checks -> comparison -> save (next version)."""
    permission = _permission()
    s = validate_akp_settings({**permission.get("akp", {}), **(settings_override or {})})
    if not s["enabled"]:
        raise RuntimeError("AKP is disabled in permission.json")
    prompts, taxonomy = _prompts(permission), _concept_taxonomy(permission)
    paths = _paths(permission)
    pdf_path = Path(pdf_path)
    docs = {d["code"].upper(): d for d in permission.get("documents", {}).values()}
    if not instrument:
        found = [d["code"] for d in docs.values() if Path(d.get("file", "")).name.lower() == pdf_path.name.lower()]
        if len(found) != 1:
            raise ValueError("Provide instrument explicitly (PDF filename is not unique)")
        instrument = found[0]
    instrument = instrument.upper()
    doc = docs.get(instrument, {"code": instrument, "name": instrument, "type": "ACT"})
    selection = scope_key or section or s["scope_key"] or s["section"] or "ALL"
    only_mkp = only_mkp or s["only_mkp"]
    session = AkpSession()
    from db_insert import get_connection, get_db_schema
    from dotenv import load_dotenv
    if paths["env_file"].exists():
        load_dotenv(paths["env_file"], override=False)
    conn = get_connection()
    store = AkpStore(conn, get_db_schema(), s["table"], s["cache_table"], s["mkp_table"])
    registry = FactRegistry(conn, get_db_schema(), s["table"].rsplit("_akp", 1)[0])
    models_api = Models(paths, store, s["use_cache"], session, fake_llm)
    embed = embed if embed is not None else make_embedder()
    short = {code: d.get("name", code) for code, d in docs.items()}
    pending_excel = [0]

    def excel(force=False):
        if not s["export_excel"]:
            return
        pending_excel[0] += 0 if force else 1
        if force or pending_excel[0] >= s["excel_every"]:
            p = excel_mod.write(paths["output_dir"] / s["excel_file"], store, registry.export_rows(), short)
            pending_excel[0] = 0
            if force:
                log(f"AKP EXCEL          : {p}")

    def judge_table(prompt, mkp_id):
        for repair in range(2):
            log(f"MODEL                  : Standard Judge ({tier_models['judge']['model']})" + (" / FORMAT RETRY" if repair else ""))
            raw = models_api.role(tier_models["judge"], prompt)
            try:
                return parse_rows(raw, mkp_id)
            except ValueError as exc:
                log(f"STANDARD FORMAT ERROR  : {exc}")
                if repair:
                    raise RuntimeError(f"Standard Judge output is not parseable: {exc}") from exc
                prompt += ("\n\nFORMAT CORRECTION REQUIRED. Return the COMPLETE final table again. Use header "
                           "MKP|TYPE|ELEMENT|CONCEPT|RESULTS. Put TC in TYPE and TC2 in ELEMENT, never TC2 in TYPE. Keep "
                           "exactly five pipe-delimited columns, every A must contain family: in RESULTS, and do not change "
                           "legal meaning. Previous error: " + str(exc) + "\nPREVIOUS RESPONSE:\n" + raw[:8000])

    def comparison(prompt):
        for repair in range(2):
            log(f"MODEL                  : MKP/AKP Comparison ({tier_models['comparison']['model']})" + (" / JSON RETRY" if repair else ""))
            raw = models_api.role(tier_models["comparison"], prompt)
            try:
                return parse_verdict(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                log(f"COMPARISON FORMAT ERROR: {exc}")
                if repair:
                    raise RuntimeError(f"Comparison output is not valid JSON: {exc}") from exc
                prompt += ("\n\nOUTPUT FORMAT FIX: return ONE JSON object only with verdict TRUE/FALSE, reconstructed_mkp, "
                           "missing, added_or_changed, logic_problems, same_matches and references. Previous error: " + str(exc))

    advisory: List[str] = []

    def checks(standard, bundle, source_text, knowledge_type):
        """Blocking errors; advisory notes go to the comparison model instead (legal_word_check)."""
        operative = knowledge_type not in NON_OPERATIVE
        reg_ids = registry.all_ids()
        errs = structure(standard, bundle["sp_rows"], operative, reg_ids)
        errs += registry.check(standard)
        if operative:
            errs += engine_ready(standard, reg_ids)
        words = legal_words(source_text, standard)
        ck = get_field(bundle["mkp_row"], "citation_key") or bundle["key"].split("#", 1)[0]
        for r in standard:
            if r["TYPE"] == "A":
                _, _, invented = refs.for_atom(get_field(r, "statement"), ck, source_text)
                for t in invented:
                    errs.append(f"REFERENCE ADDED: {r['ELEMENT']} refers to {t}, which the source MKP does not refer to")
        mode = s["legal_word_check"]
        soft = [w for w in words if mode == "WARN" or (mode == "STANDARD" and w.startswith("QUALIFIER"))]
        advisory[:] = soft
        return errs + [w for w in words if w not in soft]

    def show_false(key, attempt, source_text, standard, verdict):
        log("\n" + "=" * 72 + f"\nSTATUS                  : FALSE / {key} / ATTEMPT {attempt}\n" + "=" * 72)
        log("ORIGINAL STANDARD MKP:\n" + source_text)
        log("\nRECONSTRUCTED FROM STANDARD AKPs:\n" + (verdict.get("reconstructed_mkp") or "(not provided)"))
        log("\nSTANDARD ELEMENT STRUCTURE:")
        for r in standard:
            k = r["TYPE"]
            if k == "A":
                log(f"A  {r['ELEMENT']}: {get_field(r, 'statement')}  facts={get_field(r, 'facts')}")
            elif k == "G":
                log(f"G  {r['ELEMENT']}: {get_field(r, 'logic')} {get_field(r, 'members')}")
            elif k == "LE":
                log(f"LE {r['ELEMENT']}: {get_field(r, 'expr')}")
            elif k in ("OR", "PD", "LK"):
                log(f"{k} {r['ELEMENT']}: {r['RESULTS'][:200]}")
        log("\nWHY FALSE:")
        n = 0
        for f in ("missing", "added_or_changed", "logic_problems"):
            for reason in verdict.get(f, []):
                log(f"- {f.upper()}: {reason}")
                n += 1
        if not n:
            log("- Comparator returned FALSE without a specific reason.")
        log("=" * 72)

    def save(key, bundle, mkp_id, fingerprint, standard, vectors, tier, cache_data, tfact=None):
        mkp_version = bundle.get("mkp_version", 1)
        session.counts["atoms"] += sum(1 for r in standard if r["TYPE"] in ("A", "A_REF"))
        try:
            with conn.cursor() as cur:
                same = store.same_as_latest(cur, instrument, version, key, standard)
                if same is not None:          # identical to the saved AKP: keep that version, note the new MKP version
                    store.touch_mkp_version(cur, instrument, version, key, mkp_version, standard, vectors)
                    ver, new, edges = same, 0, 0
                    session.counts["unchanged"] += 1
                else:
                    ver = store.save_version(cur, instrument, version, key, mkp_id, mkp_version, fingerprint, standard, vectors, tier)
                    new, edges = registry.save(cur, standard, instrument, version, key, ver, embed)
                if tfact:
                    store.save_time_fact(cur, instrument, version, key, tfact)
            store.cache_put(instrument, version, key, fingerprint, {**cache_data, "akp_version": ver}, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        session.counts["new_facts"] += new
        session.counts["fact_links"] += edges
        session.counts["time_facts"] += 1 if tfact else 0
        return ver

    def same_check(key, source_text, standard, nearest):
        """Separate SAME90 call: which new atoms already exist (similarity > 0.90 candidates only)."""
        if not any(item["candidates"] for item in nearest):
            return []
        data = {"source": {"source_mkp": key, "text": source_text, "instrument": instrument},
                "new_atoms": [r for r in standard if r["TYPE"] == "A"],
                "same_candidates": [i for i in nearest if i["candidates"]]}
        prompt = prompts["same"] + "\nINPUT:\n" + json.dumps(data, ensure_ascii=False, default=str)
        for repair in range(2):
            log(f"MODEL                  : SAME check ({tier_models['same']['model']}) - "
                f"{sum(len(i['candidates']) for i in nearest)} candidate(s) > {s['same_min_similarity']}")
            raw = models_api.role(tier_models["same"], prompt)
            try:
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
                obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
                if not isinstance(obj.get("same_matches", []), list):
                    raise ValueError("same_matches must be a list")
                return obj.get("same_matches", [])
            except (ValueError, json.JSONDecodeError) as exc:
                if repair:
                    log(f"SAME CHECK FORMAT ERROR: {exc} - atoms kept as new")
                    return []
                prompt += '\n\nOUTPUT FORMAT FIX: return ONE JSON object {"same_matches": [...]} only. Error: ' + str(exc)
        return []

    from db_insert import get_db_schema as _schema
    refs = RefFinder(conn, instrument, _schema(), permission.get("ikp", {}).get("table_prefix", "ikp"))
    tier_models = s["models"]["SIMPLE"]
    try:
        store.ensure()
        registry.ensure()
        version = _version(conn, instrument, s["mkp_table"], selection)
        bundles = _read_bundles(conn, instrument, s["mkp_table"], version, selection, only_mkp)
        filled = store.backfill(instrument, embed)
        log(f"\nAKP / DOCUMENT     : {instrument}")
        log(f"AKP / SOURCE       : {s['mkp_table']} / {version}")
        log(f"AKP / SCOPE        : {selection} / {len(bundles)} MKPs")
        log(f"AKP / TABLES       : {s['table']} + {s['cache_table']} + facts + time facts")
        if filled:
            log(f"AKP / EMBEDDINGS   : {filled} pending atom embeddings filled in")
        for cluster, bundle in bundles:
            key, mkp_id = bundle["key"], bundle["mkp_id"]
            row = bundle["mkp_row"]
            source_text = get_field(row, "text")
            knowledge_type = get_field(row, "knowledge_type").upper()
            session.counts["mkps"] += 1
            log(f"\nMKP                    : {key}  [{knowledge_type}]")
            found = store.mkp_id(version, key)
            if found:
                mkp_id = found[0] or mkp_id
            base = {"source_mkp": key, "mkp_id": mkp_id, "mkp": row, "sp_rows": bundle["sp_rows"], "cluster": cluster,
                    "knowledge_type": knowledge_type,
                    "instrument": {"code": instrument, "name": doc.get("name"), "type": doc.get("type")}}
            mver = int(bundle.get("mkp_version") or 1)
            fingerprint = hashlib.sha256(json.dumps({"mkp_text": source_text, "knowledge_type": knowledge_type,
                                                     "phrases": [get_field(x, "text") for x in bundle["sp_rows"]],
                                                     "mkp_version": mver}, sort_keys=True, ensure_ascii=False,
                                                    default=str).encode()).hexdigest()
            latest = store.latest(instrument, version, key)
            # CACHE: the saved AKP was made from this same MKP version -> reuse it, no model call
            if s["use_cache"] and latest and (latest["mkp_version"] is None or int(latest["mkp_version"]) == mver):
                log(f"STATUS                  : CACHED (AKP version {latest['akp_version']}, MKP version {mver})")
                session.counts["cached"] += 1
                session.counts["atoms"] += latest.get("atoms", 0)
                continue
            reason = ("use_cache=false" if not s["use_cache"] else "no AKP saved yet" if not latest
                      else f"MKP changed (MKP v{latest['mkp_version']} -> v{mver})")
            session.reasons.append((key, reason))

            # ---- HEADING / AMENDMENT / METADATA: Python, no LLM
            if knowledge_type in PYTHON_KINDS:
                tfact = time_fact(row, instrument, key) if knowledge_type == "AMENDMENT" else None
                rows = skip_rows(bundle, knowledge_type, tfact)
                ver = save(key, bundle, mkp_id, fingerprint, rows, {}, "PYTHON", {"status": "APPROVED", "python": True,
                                                                                  "standard": rows}, tfact)
                session.counts["python_written"] += 1
                log(f"STATUS                  : TRUE (PYTHON {knowledge_type}, version {ver})"
                      + (f" - time fact {tfact['kind']} {tfact['effective_from'] or ''}" if tfact else ""))
                excel()
                continue

            tier, score = complexity(source_text, bundle["sp_rows"], knowledge_type, s["complex_threshold"])
            tier_models = s["models"][tier]
            log(f"TIER                   : {tier} (score {score}) - judge {tier_models['judge']['model']}, "
                f"comparison {tier_models['comparison']['model']}")

            m = re.match(r"^([A-Z0-9]+_(?:S|R|SCH|PA|AR|EN|O)[0-9A-Z]*)", key.split("#", 1)[0])
            unit_prefix = m.group(1) if m else key.split("#", 1)[0]          # facts already used in this section
            try:
                mkp_vec = embed(source_text)
            except Exception:
                mkp_vec = None
            input_data = {**base, "fact_registry": registry.slice(unit_prefix, mkp_vec, s["fact_slice"])}
            approved, feedback, last_error = False, "", ""
            for attempt in range(1, s["max_attempts"] + 1):
                gemini_raw = claude_raw = ""
                standard, gemini, claude, nearest, vectors = [], [], [], [], {}
                payload = json.dumps(input_data, ensure_ascii=False, default=str)
                if feedback:
                    payload += "\nPRIOR COMPARISON FALSE - CORRECT THESE ISSUES:\n" + feedback
                log(f"ATTEMPT                : {attempt}/{s['max_attempts']}")
                try:
                    gen_prompt = (prompts["akp"] + "\nTAXONOMY GUIDANCE (NOT A CLASSIFICATION DECISION):\n" + taxonomy +
                                  "\nINPUT:\n" + payload)
                    log(f"MODEL                  : AKP {tier_models['generator_1']['model']} + {tier_models['generator_2']['model']}")
                    gemini_raw, claude_raw = models_api.pair(tier_models, gen_prompt)
                    gemini, claude = raw_rows(gemini_raw, mkp_id), raw_rows(claude_raw, mkp_id)
                    judge_data = {"source": input_data, "gemini": gemini_raw, "claude": claude_raw}
                    if feedback:                    # the judge sees why the comparison said FALSE last time
                        judge_data["previous_comparison_false"] = json.loads(feedback)
                    judge_input = json.dumps(judge_data, ensure_ascii=False, default=str)
                    judge_prompt = (prompts["judge"] + "\n\nTAXONOMY GUIDANCE (DO NOT ASSIGN SUBGROUPS):\n" + taxonomy +
                                    "\nRUNTIME NOTE: gemini and claude inputs are RAW candidate responses. They may have "
                                    "missing headers or minor TYPE/row formatting mistakes. Interpret and correct these "
                                    "while judging. Return one COMPLETE, valid five-column Standard table.\nINPUT:\n" + judge_input)
                    standard = judge_table(judge_prompt, mkp_id)
                    errs = checks(standard, bundle, source_text, knowledge_type)
                    for repair in range(s["judge_repairs"]):
                        if not errs:
                            break
                        log(f"PYTHON CHECK           : {len(errs)} issue(s) - back to the judge")
                        for e in errs[:6]:
                            log(f"  - {e[:180]}")
                        standard = judge_table(judge_prompt + "\n\nPYTHON_CHECK_FAILED - fix exactly these items:\n" +
                                               "\n".join(f"- {e}" for e in errs) + "\n\nYOUR PREVIOUS TABLE:\n" +
                                               table_text(standard), mkp_id)
                        errs = checks(standard, bundle, source_text, knowledge_type)
                    if errs:
                        verdict = {"verdict": "FALSE", "reconstructed_mkp": "(not compared - Python checks failed)",
                                   "missing": [e for e in errs if e.startswith(("MISSING", "NUMBER LOST", "QUALIFIER"))],
                                   "added_or_changed": [e for e in errs if e.startswith(("NEGATION", "MODAL", "NUMBER ADDED"))],
                                   "logic_problems": [e for e in errs if not e.startswith(("MISSING", "NUMBER", "QUALIFIER",
                                                                                           "NEGATION", "MODAL"))],
                                   "same_matches": [], "references": []}
                    else:
                        cmp_data = {"source": input_data, "standard": standard}
                        if advisory:                # e.g. a qualifier Python could not find in words - you decide
                            cmp_data["python_notes"] = advisory
                        cmp_input = json.dumps(cmp_data, ensure_ascii=False, default=str)
                        verdict = comparison(prompts["comparison"] + "\nINPUT:\n" + cmp_input)
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"TECHNICAL ERROR        : {last_error}")
                    store.cache_put(instrument, version, key, fingerprint,
                                    {"status": "TECHNICAL_ERROR", "attempt": attempt, "error": last_error, "gemini": gemini,
                                     "claude": claude, "standard": standard, "gemini_raw": gemini_raw, "claude_raw": claude_raw})
                    continue                                        # next attempt (old engine stopped here)

                if verdict["verdict"] == "TRUE":
                    # references: Python, from each atom's own words + the IKP cross-references
                    ck = get_field(row, "citation_key") or key.split("#", 1)[0]
                    legal = 0
                    for r in standard:
                        if r["TYPE"] == "A":
                            labels, keys, _ = refs.for_atom(get_field(r, "statement"), ck, source_text)
                            set_references(r, labels, keys)
                            legal += len(labels)
                    # SAME: separate check against existing atoms (cosine > 0.90), SAME90 prompt
                    vectors, nearest = {}, []
                    try:
                        vectors = _vectors(standard, embed, session)
                        nearest = store.same_candidates(key, standard, vectors, min_similarity=s["same_min_similarity"])
                        matches = same_check(key, source_text, standard, nearest)
                    except Exception as exc:
                        log(f"SAME CHECK SKIPPED      : {type(exc).__name__}: {str(exc)[:150]} - atoms kept as new")
                        matches = []
                    same_n = _apply_same(standard, matches, nearest, s["same_min_similarity"])
                    vectors = {i: v for i, v in vectors.items() if standard[i]["TYPE"] == "A"}
                    ver = save(key, bundle, mkp_id, fingerprint, standard, vectors, tier,
                               {"status": "APPROVED", "attempt": attempt, "gemini": gemini, "claude": claude,
                                "standard": standard, "comparison": verdict, "tier": tier})
                    session.counts["approved"] += 1
                    session.counts["same_refs"] += same_n
                    session.counts["legal_refs"] += legal
                    log(f"STATUS                  : TRUE (version {ver})" + (f" - {same_n} atom(s) SAME as existing" if same_n else ""))
                    approved = True
                    excel()
                    break
                show_false(key, attempt, source_text, standard, verdict)
                feedback = json.dumps(verdict, ensure_ascii=False)
                store.cache_put(instrument, version, key, fingerprint,
                                {"status": "FAILED", "attempt": attempt, "gemini": gemini, "claude": claude,
                                 "standard": standard, "comparison": verdict})
            if not approved:
                session.counts["failed"] += 1
                session.failures.append(key)
                _log_failure(paths, instrument, key, last_error or feedback)
        excel(force=True)
        c = session.counts
        llm_done = c["approved"] + c["failed"]
        say(f"  AKP : {c['mkps']} MKP(s) - {c['cached']} from database, {llm_done} by LLM, {c['python_written']} by Python "
            f"(headings / amendments) - {c['atoms']} atoms - LLM calls {c['llm_calls']}")
        for key, reason in session.reasons:
            say(f"        LLM for {key}: {reason}")
        if session.failures:
            say("  AKP REVIEW REQUIRED : " + ", ".join(session.failures))
        log("AKP COUNTS " + json.dumps(c))
        return session
    finally:
        conn.close()


def _log_failure(paths: dict, instrument: str, key: str, reason: str) -> None:
    out = Path(paths["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    with (out / "akp_failed_mkps.txt").open("a", encoding="utf-8") as fh:
        fh.write(f"{instrument} | {key} | {' '.join(str(reason).split())[:1500]}\n")


def main_akp():
    permission = _permission()
    settings = validate_akp_settings(permission.get("akp", {}))
    if not settings["enabled"]:
        log("AKP : SKIPPED")
        return []
    from kp_settings import selected_documents
    documents = selected_documents(permission)
    if not documents:
        raise RuntimeError("No PDF selected: check \"read\" in permission.json and the Law sub-folders")
    selection = settings["scope_key"] or settings["section"]
    if not selection and settings["ask_user_input"]:
        selection = input("AKP section number / citation key / ALL: ").strip() or "ALL"
    selection = selection or "ALL"
    from kp_settings import resolve
    law_dir = resolve(permission["paths"]["law_dir"])
    return [run_akp_scope(law_dir / d["file"], d["code"], scope_key=selection, only_mkp=settings["only_mkp"])
            for d in documents]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DARP LAW ARC AKP")
    parser.add_argument("--pdf", help="PDF filename (optional: use permission.json selected documents)")
    parser.add_argument("--instrument", help="Legal document code such as CGSTA17")
    parser.add_argument("--section", help="Section 7 or explicit citation key")
    parser.add_argument("--mkp", help="One MKP ID or citation key")
    args = parser.parse_args()
    if args.pdf:
        run_akp_scope(BASE_DIR / args.pdf, args.instrument, args.section, only_mkp=args.mkp)
    else:
        main_akp()
