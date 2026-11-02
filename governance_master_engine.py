"""DARP LAW ARC - GOVERNANCE MASTER: one governance master per taxonomy sub-group (TSG), kept up to date as laws arrive.

A governance master holds everything the Decision Graph Engine needs for one TSG: its laws, the laws they depend on
(one step only), the spine ("is it a supply?", goods or services), facts, checks, groups, outcomes, products and steps.

    Governance_Master/                (outside Output/)
      index.xlsx                      every governance master: name, status, number of laws, last changed
      TG01/TSG01_03/rulebook.yaml     the file the engine reads (the engine requires this file name)
      TG01/TSG01_03/report.xlsx       why each law is in, what was left out, what to review
    PostgreSQL
      governance_master               the current content of every governance master, one row per element
      governance_master_history       one row per change (old content is never lost)

When it runs: right after GKP of the scope you chose, for every TSG that scope's atoms were grouped in - no question
asked. Alone: python governance_master_engine.py [--tsg TSG01_03] [--all] [--views]
A TSG always gets a governance master, even when its laws are already covered by the spine.
If the result is the same as the saved one, nothing is rewritten. No LLM and no engine are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from governance_master_ss import excel as excel_mod
from governance_master_ss import pack as pack_mod
from governance_master_ss.build import Built, GovernanceMasterError, build
from governance_master_ss.source import Snapshot, from_db
from governance_master_ss.store import GovernanceMasterStore
from kp_console import log, say

BASE_DIR = Path(__file__).resolve().parent
REQUIRED = ("enabled", "use_cache", "save_to_db", "tsgs", "table", "history_table",
            "output_dir", "spine_rulebook", "map_file", "prerequisite_depth", "tables")
PACK_FILE = "rulebook.yaml"            # the file name the Decision Graph Engine looks for inside a pack folder


def _permission() -> dict:
    from kp_settings import load
    return load()


def _path(v) -> Path:
    p = Path(str(v))
    return p if p.is_absolute() else BASE_DIR / p


def validate_governance_master_settings(raw: dict) -> dict:
    s = dict(raw or {})
    missing = [k for k in REQUIRED if k not in s]
    if missing:
        raise KeyError("governance_master settings are missing: " + ", ".join(missing))
    for k in ("enabled", "use_cache", "save_to_db"):
        if type(s[k]) is not bool:
            raise ValueError(f"governance_master.{k} must be true or false")
    sg = s["tsgs"]
    if not (sg in ("TOUCHED", "ALL") or (isinstance(sg, list) and sg and all(isinstance(x, str) for x in sg))):
        raise ValueError('permission.json governance_master.tsgs must be "TOUCHED", "ALL" or a list such as ["TSG01_03"]')
    s["merge_clauses"] = bool(s.get("merge_clauses", True))
    s["prerequisite_depth"] = int(s["prerequisite_depth"])
    s["law_as_on"] = str(s.get("law_as_on") or "").strip()
    for k in ("akp", "fact", "gkp", "time_fact", "mkp", "ikp"):
        if k not in s["tables"]:
            raise KeyError(f"Config/settings.json governance_master.tables.{k} is required")
    return s


def _sha(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def taxonomy_names(permission: dict) -> Dict[str, dict]:
    path = _path(permission.get("gkp", {}).get("taxonomy_file", ""))
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out = {}
    for g in data["groups"]:
        for sub in g["subgroups"]:
            out[sub["tsg_id"]] = {"tsg": sub["tsg_id"], "tsg_name": sub["name"], "tg": g["tg_id"],
                                       "tg_name": g["name"]}
    return out


def fingerprint(snapshot: Snapshot, built: Built, tsg: str, cfg: dict, spine: dict, s: dict) -> str:
    """What THIS governance master is built from - only its own laws, their facts, its GKP rows, the time facts and
    questions of those laws, the spine, the mapping and the settings. A law added to another sub-group does not
    change it."""
    keys = set(built.included) | set(built.report.get("covered_by_spine", []))
    rows = [r for r in snapshot.akp if r["source_mkp"] in keys]
    facts = {f: snapshot.facts.get(f) for f in built.rulebook.get("facts", {}) if f in snapshot.facts}
    gkp = sorted((g["source_mkp"], g.get("akp_element"), g.get("status")) for g in snapshot.gkp if g.get("tsg_id") == tsg)
    cites = {r["source_mkp"].split("#", 1)[0] for r in rows}
    times = [t for t in snapshot.time_facts if any(str(t.get("target_citation_key", "")).startswith(c) or
                                                   c.startswith(str(t.get("target_citation_key", ""))) for c in cites)]
    qs = {k: v for k, v in snapshot.questions.items() if k in keys}
    return _sha(rows, facts, gkp, times, qs, cfg, spine, s["prerequisite_depth"], s["law_as_on"], tsg)


def pack_folder(s: dict, names: dict) -> Path:
    return _path(s["output_dir"]) / names["tg"] / names["tsg"]


def _in_scope(mkp: str, scope_key: str) -> bool:
    cite = mkp.split("#", 1)[0]
    return cite == scope_key or cite.startswith(scope_key + "_")


# ---------------------------------------------------------------- which governance masters to (re)build

def tsgs_with_atoms(conn, schema: str, s: dict) -> List[str]:
    from psycopg2 import sql
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT DISTINCT tsg_id FROM {}.{} WHERE is_latest AND status='APPROVED' "
                            "AND COALESCE(tsg_id, '') <> '' ORDER BY 1").format(sql.Identifier(schema),
                                                                                sql.Identifier(s["tables"]["gkp"])))
        return [r[0] for r in cur.fetchall()]


def tsgs_for_mkps(snapshot: Snapshot, source_mkps: List[str], store: Optional[GovernanceMasterStore]) -> List[str]:
    """The TSGs these MKPs' atoms are grouped in, plus governance masters that already use these MKPs."""
    wanted = set(source_mkps)
    out = []
    for g in snapshot.gkp:
        if str(g.get("status", "")).upper() == "APPROVED" and g.get("tsg_id") and g["source_mkp"] in wanted:
            if g["tsg_id"] not in out:
                out.append(g["tsg_id"])
    for tsg, lp in (store.latest_all() if store else {}).items():
        if tsg not in out and wanted & set(lp["laws"]):
            out.append(tsg)
    return sorted(out)


# ---------------------------------------------------------------- one governance master

def _comment_partners(text: str, partners: List[dict]) -> str:
    """Write next to each spine check which of your atoms it stands for (yaml comment, the engine ignores it)."""
    by = {}
    for p in partners:
        by.setdefault(p["spine_id"], []).append(f"{p['source_mkp']}::{p['atom']}")
    for sid, atoms in by.items():
        text = re.sub(rf"^(  {re.escape(sid)}:)(.*)$", lambda m: f"{m.group(1)}{m.group(2)}  # stands for {', '.join(atoms)}",
                      text, count=1, flags=re.M)
    return text


def build_governance_master(tsg: str, snapshot: Snapshot, conn, permission: dict, s: dict) -> dict:
    names = taxonomy_names(permission)
    if tsg not in names:
        raise GovernanceMasterError(f"{tsg} is not a TSG of {permission['gkp']['taxonomy_file']}")
    n = names[tsg]
    cfg = json.loads(_path(s["map_file"]).read_text(encoding="utf-8-sig"))
    cfg["_merge_clauses"] = s["merge_clauses"]
    spine = yaml.safe_load(_path(s["spine_rulebook"]).read_text(encoding="utf-8")) or {}
    from db_insert import get_db_schema
    store = GovernanceMasterStore(conn, get_db_schema(), s["table"], s["history_table"]) if conn is not None and s["save_to_db"] else None
    if store:
        store.ensure()
    say(f"  {tsg} {n['tsg_name']} : constructing")
    built = build(snapshot, tsg, n["tsg_name"], n["tg"], n["tg_name"], cfg, spine, f"{tsg}", s["prerequisite_depth"],
                  s["law_as_on"])
    rep = built.report
    fp = fingerprint(snapshot, built, tsg, cfg, spine, s)
    folder = pack_folder(s, n)
    latest = (store.latest_all().get(tsg) if store else None)
    counts = rep.get("counts", {})
    size = (f"{counts.get('compounds', 0)} steps, {counts.get('predicates', 0)} checks, {counts.get('facts', 0)} facts | "
            f"laws: {len(rep['members'])} member, {len(rep['added'])} linked, {len(rep['covered_by_spine'])} in the spine"
            + (f", {len(rep['excluded'])} left out" if rep["excluded"] else ""))
    if s["use_cache"] and latest and latest["fingerprint"] == fp and (folder / PACK_FILE).is_file():
        say(f"  {tsg} : unchanged (same laws as before) - {size}")
        return {"tsg": tsg, "status": "UNCHANGED", "pack_status": latest["pack_status"], "counts": counts}
    problems = pack_mod.check_pack(built.rulebook)
    status = "APPROVED" if not problems and not rep["blocking"] else "NEEDS_REVIEW"
    text = (f"# GOVERNANCE MASTER {tsg} {n['tsg_name']} ({n['tg']} {n['tg_name']})\n"
            f"# LAW ARC - {len(built.included)} laws + the spine - status {status}\n"
            + _comment_partners(yaml.safe_dump(built.rulebook, sort_keys=False, allow_unicode=True, width=120),
                                rep.get("spine_partners", [])))
    rows = pack_mod.rows(built.rulebook, rep, built.id_map, spine, n, status)
    partners = {}
    for x in rep.get("spine_partners", []):
        partners.setdefault(x["spine_id"], []).append(x["akp_id"] or f"{x['source_mkp']}::{x['atom']}")
    for r in rows:
        if r.get("element_id") in partners:
            r["attributes"] = dict(r.get("attributes") or {}, stands_for=partners[r["element_id"]])
    old = store.current_rows(tsg) if store else {}
    ch = pack_mod.change(old, rows)
    file_changed = not (folder / PACK_FILE).is_file() or \
        re.sub(r"^#.*\n", "", (folder / PACK_FILE).read_text(encoding="utf-8"), flags=re.M) != \
        re.sub(r"^#.*\n", "", text, flags=re.M)
    folder.mkdir(parents=True, exist_ok=True)
    if file_changed or not s["use_cache"]:
        (folder / PACK_FILE).write_text(text, encoding="utf-8")
    excel_mod.write(folder / "report.xlsx", built.rulebook, rep, built.id_map, problems, status)
    version = None
    if store and (ch["added"] or ch["changed"] or ch["removed"] or not latest or not s["use_cache"]
                  or latest["fingerprint"] != fp):
        version = store.save(tsg, rows, {"fingerprint": fp, "pack_status": status, "tsg_name": n["tsg_name"],
                                         "tg_id": n["tg"], "tg_name": n["tg_name"],
                                         "laws": [x["mkp"] for x in rep["included"]] + list(rep["covered_by_spine"]),
                                         "change": ch, "payload": text, "report": {**rep, "problems": problems}})
        from governance_master_ss.views import ensure_views
        ensure_views(conn, get_db_schema(), s["tables"], s["table"])
    say(f"  {tsg} : done - {size} - {status}"
        + (f" ({len(problems) + len(rep['blocking'])} to review, see report.xlsx)" if status != "APPROVED" else ""))
    for p_ in (rep["blocking"] + problems)[:3]:
        say(f"        review: {p_}")
    for p_ in (rep["blocking"] + problems)[3:]:
        log(f"REVIEW {tsg}: {p_}")
    for w in rep.get("warnings", []):
        log(f"NOTE {tsg}: {w}")
    log(f"CHANGE {tsg}: {len(ch['added'])} added, {len(ch['changed'])} changed, {len(ch['removed'])} removed | "
        f"{folder / PACK_FILE}" + (f" | history v{version}" if version else ""))
    return {"tsg": tsg, "status": "WRITTEN", "pack_status": status, "folder": str(folder), "laws": len(built.included),
            "change": ch, "problems": problems, "version": version, "counts": counts}


def write_index(s: dict, store: Optional[GovernanceMasterStore], permission: dict) -> Optional[Path]:
    if not store:
        return None
    names = taxonomy_names(permission)
    packs = []
    for tsg, lp in sorted(store.latest_all().items()):
        n = names.get(tsg, {"tsg": tsg, "tsg_name": lp.get("tsg_name"), "tg": lp.get("tg"), "tg_name": lp.get("tg_name")})
        packs.append({"tsg": tsg, **lp, "tg": n["tg"], "tg_name": n["tg_name"],
                      "file": str(pack_folder(s, n) / PACK_FILE)})
    return excel_mod.write_index(_path(s["output_dir"]) / "index.xlsx", packs)


def build_many(tsgs: List[str], permission: Optional[dict] = None, conn=None) -> List[dict]:
    """Build / refresh these governance masters one after another (TSG01_03 : constructing ... done)."""
    permission = permission or _permission()
    s = validate_governance_master_settings(permission.get("governance_master", {}))
    if not s["enabled"] or not tsgs:
        return []
    from db_insert import get_connection, get_db_schema
    own = conn is None
    conn = conn or get_connection()
    try:
        codes = list(permission.get("documents", {}))
        snapshot = from_db(conn, get_db_schema(), s["tables"], codes)
        say(f"\nGOVERNANCE MASTER : {len(tsgs)} TSG(s) - {', '.join(tsgs)}")
        results = []
        for tsg in tsgs:
            try:
                results.append(build_governance_master(tsg, snapshot, conn, permission, s))
            except GovernanceMasterError as exc:
                say(f"  {tsg} : not built - {exc}")
                results.append({"tsg": tsg, "status": "SKIPPED", "reason": str(exc)})
        store = GovernanceMasterStore(conn, get_db_schema(), s["table"], s["history_table"]) if s["save_to_db"] else None
        if results and store:
            log(f"GOVERNANCE MASTER INDEX : {write_index(s, store, permission)}")
        return results
    finally:
        if own:
            conn.close()


def run_for_mkps(source_mkps: List[str], permission: Optional[dict] = None) -> List[dict]:
    """After GKP: every TSG the atoms of these MKPs were grouped in (no question asked)."""
    permission = permission or _permission()
    s = validate_governance_master_settings(permission.get("governance_master", {}))
    if not s["enabled"] or not source_mkps:
        return []
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        store = GovernanceMasterStore(conn, get_db_schema(), s["table"], s["history_table"]) if s["save_to_db"] else None
        if store:
            store.ensure()
        if s["tsgs"] == "ALL":
            tsgs = tsgs_with_atoms(conn, get_db_schema(), s)
        elif isinstance(s["tsgs"], list):
            tsgs = list(s["tsgs"])
        else:
            snapshot = from_db(conn, get_db_schema(), s["tables"], list(permission.get("documents", {})))
            tsgs = tsgs_for_mkps(snapshot, source_mkps, store)
        return build_many(tsgs, permission, conn)
    finally:
        conn.close()


def main_governance_master(tsgs: Optional[List[str]] = None, all_tsgs: bool = False) -> List[dict]:
    """Alone: the listed TSGs, or every TSG that has atoms (--all)."""
    permission = _permission()
    s = validate_governance_master_settings(permission.get("governance_master", {}))
    from db_insert import get_connection, get_db_schema
    conn = get_connection()
    try:
        if not tsgs:
            tsgs = tsgs_with_atoms(conn, get_db_schema(), s) if all_tsgs or s["tsgs"] in ("ALL", "TOUCHED") else list(s["tsgs"])
        return build_many(tsgs, permission, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DARP LAW ARC GOVERNANCE MASTER - one governance master per TSG")
    ap.add_argument("--tsg", action="append", help="build these TSGs now (repeatable), e.g. --tsg TSG01_03")
    ap.add_argument("--all", action="store_true", help="every TSG that has approved atoms")
    ap.add_argument("--views", action="store_true", help="only (re)create v_molecule_elements and v_element_links")
    args = ap.parse_args()
    if args.views:
        from db_insert import get_connection, get_db_schema
        from governance_master_ss.views import ensure_views
        _s = validate_governance_master_settings(_permission().get("governance_master", {}))
        _c = get_connection()
        try:
            say("VIEWS : " + ", ".join(ensure_views(_c, get_db_schema(), _s["tables"], _s["table"])))
        finally:
            _c.close()
    else:
        main_governance_master(args.tsg, args.all)
