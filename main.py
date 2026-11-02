"""DARP LAW ARC: IKP -> MKP -> AKP -> GKP -> GOVERNANCE MASTER.

You choose the scope once per document (permission.json scope.ask_user_input). After that everything runs by itself:
each unit goes MKP -> AKP -> GKP as soon as it is ready, and when the scope is done every TSG its atoms were grouped
in gets its governance master. Then the next document. No question is asked at the end.
The screen shows the important lines; the full detail is in Output/logs/.
"""
from __future__ import annotations

from typing import Dict, List

import kp_settings
from kp_console import log, log_path, say


def main_ikp(cfg: dict) -> List[dict]:
    from ikp_engine import run_ikp_pipeline, validate_ikp_settings
    settings = validate_ikp_settings(cfg["ikp"])
    paths = kp_settings.build_paths(cfg)
    results = []
    for document in kp_settings.selected_documents(cfg):
        path = paths["law_dir"] / document["file"]
        if not path.is_file():
            say(f"IKP : PDF missing {path}")
            continue
        try:
            result = run_ikp_pipeline(path, settings, document, paths)
        except Exception as exc:                 # one bad PDF must not stop the other documents
            say(f"IKP : {document['code']} FAILED - skipped, the other documents go on ({str(exc)[:200]})")
            log(f"IKP {document['code']} FAILED: {exc}")
            continue
        results.append(result)
        log(f"IKP {document['code']}: {result['ikp_rows']} rows, LLM calls {result['llm_calls']}")
    return results


class ScopeSummary:
    """Counts for the end-of-scope summary (per document)."""

    def __init__(self):
        self.akp_atoms = 0
        self.akp_mkps = 0
        self.akp_llm = 0
        self.akp_cached = 0
        self.gkp_llm = 0
        self.tsg: Dict[str, dict] = {}
        self.unclassified = 0
        self.source_mkps: List[str] = []

    def add_akp(self, session) -> None:
        c = getattr(session, "counts", {}) or {}
        self.akp_atoms += c.get("atoms", 0)
        self.akp_mkps += c.get("mkps", 0)
        self.akp_cached += c.get("cached", 0)
        self.akp_llm += c.get("llm_calls", 0)

    def add_gkp(self, counts: dict) -> None:
        self.gkp_llm += counts.get("llm_calls", 0)
        self.unclassified += counts.get("unclassified", 0)
        for m in counts.get("source_mkps", []):
            if m not in self.source_mkps:
                self.source_mkps.append(m)
        for tsg, v in (counts.get("tsg_counts") or {}).items():
            cur = self.tsg.setdefault(tsg, {"name": v["name"], "atoms": 0})
            cur["atoms"] += v["atoms"]


def show_summary(doc_result: dict, s: ScopeSummary, gm_results: List[dict]) -> None:
    say("\n" + "=" * 72)
    say(f"SUMMARY - {doc_result.get('name', doc_result['document'])} - {doc_result.get('scope_label', '')}")
    say(f"  MKP  : {doc_result.get('mkps', 0)} MKP(s) - {doc_result.get('mkps_from_cache', 0)} from database, "
        f"{doc_result.get('mkps', 0) - doc_result.get('mkps_from_cache', 0)} by LLM "
        f"({doc_result.get('mkps_new', 0)} new, {doc_result.get('mkps_changed', 0)} new version)"
        + (f" - {doc_result.get('failed', 0)} unit(s) FAILED" if doc_result.get("failed") else ""))
    say(f"  AKP  : {s.akp_atoms} atoms from {s.akp_mkps} MKP(s) ({s.akp_cached} MKP(s) from database)")
    if s.tsg:
        say("  GKP  : atoms per TSG")
        for tsg, v in sorted(s.tsg.items()):
            say(f"         {tsg}  {v['name'][:44]:<44} : {v['atoms']}")
    if s.unclassified:
        say(f"         UNCLASSIFIED : {s.unclassified} atom(s) - see Output/gkp.xlsx")
    if gm_results:
        say("  GOVERNANCE MASTER")
        for r in gm_results:
            c = r.get("counts") or {}
            say(f"         {r['tsg']} : {r['status'].lower()}"
                + (f" - {c.get('compounds', 0)} steps, {c.get('predicates', 0)} checks" if c else "")
                + (f" - {r.get('pack_status')}" if r.get("pack_status") else ""))
    say(f"  LLM calls : MKP {doc_result.get('llm_calls', 0)}, AKP {s.akp_llm}, GKP {s.gkp_llm}")
    say(f"  Full detail : {log_path()}")
    say("=" * 72)


def main() -> None:
    cfg = kp_settings.load()
    from config_check import check_permission
    check_permission(cfg)                 # stops here, naming every missing key, before any step runs
    run = {k: cfg[k]["enabled"] for k in kp_settings.STEPS}
    docs = kp_settings.selected_documents(cfg)
    say("\nDARP LAW ARC")
    say("=" * 60)
    say("STEPS     : " + " -> ".join(k.upper().replace("_", " ") for k in kp_settings.STEPS if run[k]))
    say("DOCUMENTS : " + (", ".join(f"{d['name']} ({d['code']})" for d in docs) or "none - check \"read\" in permission.json"))
    say(f"LOG       : {log_path()}")
    if run["ikp"]:
        main_ikp(cfg)
    if not run["mkp"]:
        say("MKP : switched off - AKP / GKP / governance master use the MKPs already saved")
        _without_mkp(cfg, run)
        return
    summaries: Dict[str, ScopeSummary] = {}

    def on_unit(result: dict) -> None:
        instrument, scope_key = result["document"], result["scope_key"]
        s = summaries.setdefault(instrument, ScopeSummary())
        if run["akp"]:
            from akp_engine import run_akp_scope
            session = run_akp_scope(result["pdf_path"], instrument=instrument, scope_key=scope_key)
            s.add_akp(session)
            if getattr(session, "failures", None):
                say(f"  GKP : runs for the MKPs AKP approved; left out until AKP passes: {', '.join(session.failures)}")
        if run["gkp"]:
            from gkp_engine import run_gkp_scope
            s.add_gkp(run_gkp_scope(instrument=instrument, scope_key=scope_key) or {})

    def on_document(doc_result: dict) -> None:
        s = summaries.setdefault(doc_result["document"], ScopeSummary())
        gm = []
        if run["governance_master"] and s.source_mkps:
            from governance_master_engine import run_for_mkps
            gm = run_for_mkps(s.source_mkps, cfg)
        show_summary(doc_result, s, gm)

    from mkp_engine import main_mkp
    main_mkp(on_scope_complete=on_unit, on_document_complete=on_document)


def _without_mkp(cfg: dict, run: dict) -> None:
    """MKP off: AKP / GKP / governance master on the saved MKPs of the default scope."""
    scope = cfg["mkp"].get("scope") or "ALL"
    scope = "ALL" if str(scope).upper() in ("FULL", "ALL") else scope
    paths = kp_settings.build_paths(cfg)
    mkps: List[str] = []
    for d in kp_settings.selected_documents(cfg):
        if run["akp"]:
            from akp_engine import run_akp_scope
            run_akp_scope(paths["law_dir"] / d["file"], d["code"], scope_key=scope)
        if run["gkp"]:
            from gkp_engine import run_gkp_scope
            mkps += (run_gkp_scope(d["code"], scope_key=scope) or {}).get("source_mkps", [])
    if run["governance_master"]:
        from governance_master_engine import main_governance_master, run_for_mkps
        if mkps:
            run_for_mkps(mkps, cfg)
        elif not run["gkp"]:
            main_governance_master()


if __name__ == "__main__":
    main()
