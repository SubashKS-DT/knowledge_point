"""DARP LAW ARC - IKP (IKP): the book index of a GST legal document.

Flow for one document (Act / Rules / Circular / Notification / Order ...):
  1. Read the PDF (layout-aware: headers, footers, contents pages, footnotes, margin notes handled).
  2. Python splits it chapter -> section/rule -> sub-section -> clause -> sub-clause -> proviso /
     explanation / closing text / table rows, with exact text, page + bbox blocks and anchors.
  3. For each chapter: quality gate -> (only if it FAILS, or llm.mode=ALWAYS) LLM line-tagging assist
     -> gate again -> keep the better version -> COMMIT the chapter to the DB (checkpoint).
  4. Document-level gate, then the version is marked COMPLETE and is_latest.

Switches (permission.json "ikp"):
  enabled=false               -> nothing is read; the latest saved index is used.
  use_cache=true              -> same PDF already COMPLETE -> reuse; a RUNNING version of the same PDF -> resume
                                 from the first chapter not yet committed; otherwise a new version.
  use_cache=false             -> always read again as a NEW version (old versions are kept).
  llm.mode = OFF | ON_FAIL | ALWAYS ;  llm.provider = CLAUDE | GEMINI ;  llm.model = model id
Supporting code lives in the ikp_ss/ folder.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ikp_ss.build import (PARSER_VERSION, analyse, chapter_groups, document_quality, document_records,
                            file_sha256, group_records, now_iso, re_enrich, write_jsonl)
from ikp_ss.gates import evaluate_group
from kp_console import log, say
from ikp_ss import llm_assist, store as store_mod

BASE_DIR = Path(__file__).resolve().parent
COLUMNS = ("DOC", "TYPE", "ELEMENT", "PAGE_NUMBER", "RESULTS")
ROW_TYPES = {"DOC", "NODE", "REF", "XREF", "DEF", "SUM", "C", "CM", "UNC"}
LLM_MODES = {"OFF", "ON_FAIL", "ALWAYS"}


def validate_ikp_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("permission.json: ikp must be a JSON object")
    out = dict(settings)
    for key, default in (("enabled", True), ("use_cache", True), ("save_to_db", True), ("export_excel", True),
                         ("export_jsonl", True), ("ask_user_input", False), ("fail_on_gate", True),
                         ("dry_run_fetch", True)):
        value = out.get(key, default)
        if type(value) is not bool:
            raise ValueError(f"ikp.{key} must be true or false")
        out[key] = value
    if out.get("force_reindex") is True:  # old name of use_cache=false
        out["use_cache"] = False
    if "max_retries" not in out:
        raise KeyError("permission.json: ikp.max_retries is required")
    out["max_retries"] = int(out["max_retries"])
    if out["max_retries"] < 0:
        raise ValueError("ikp.max_retries must be >= 0")
    out["excel_file"] = str(out.get("excel_file", "ikp.xlsx")).strip()
    if not out["excel_file"].lower().endswith(".xlsx"):
        raise ValueError("ikp.excel_file must end in .xlsx")
    out["schema"] = str(out.get("schema", "public")).strip()
    out["table_prefix"] = str(out.get("table_prefix", "ikp")).strip()
    for value in (out["schema"], out["table_prefix"]):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"Invalid SQL identifier: {value}")
    out["jsonl_dir"] = str(out.get("jsonl_dir", "Output/ikp"))
    out["prompt_file"] = str(out.get("prompt_file", "law_arc_ikp_map_prompt.txt"))
    llm = dict(out.get("llm") or {})
    mode = str(llm.get("mode", "ON_FAIL")).upper()
    if mode not in LLM_MODES:
        raise ValueError(f"ikp.llm.mode must be one of {sorted(LLM_MODES)}")
    provider = str(llm.get("provider", "CLAUDE")).upper()
    if provider not in {"CLAUDE", "GEMINI"}:
        raise ValueError("ikp.llm.provider must be CLAUDE or GEMINI")
    chapters = llm.get("chapters") or []
    if not isinstance(chapters, list):
        raise ValueError("ikp.llm.chapters must be a list of chapter keys, e.g. [\"CGSTR17_CHIII\"]")
    out["llm"] = {"mode": mode, "provider": provider, "model": str(llm.get("model") or "").strip(),
                  "chapters": [str(c).strip() for c in chapters]}
    out["stop_after_chapters"] = int(out.get("stop_after_chapters", 0) or 0)  # testing: simulate a crash
    return out


def get_document_context(document: dict) -> Tuple[str, str, str]:
    values = tuple(str(document.get(key, "")).strip() for key in ("name", "code", "type"))
    if not all(values):
        raise ValueError("Document name, code and type are required (Config/documents.json)")
    return values[0], values[1].upper(), values[2].upper()


def get_connection(paths: Optional[dict] = None):
    """PostgreSQL from Credentials/.env (LEGAL_DB_HOST / PORT / NAME / USER / PASSWORD); upgrades old table names once."""
    from dotenv import load_dotenv
    if paths and paths.get("env_file") and Path(paths["env_file"]).is_file():
        load_dotenv(paths["env_file"], override=False)
    from db_insert import get_connection as _connect
    return _connect()


def _llm_caller(settings: dict, paths: dict):
    runtime = {}

    def call(prompt: str) -> str:
        from llm_runtime import call_llm, create_llm_runtime
        if not runtime:
            runtime.update(create_llm_runtime(settings["llm"]["provider"], paths))
            if settings["llm"]["model"]:
                runtime["model"] = settings["llm"]["model"]
        text, _ = call_llm(runtime, prompt)
        return text
    return call


def _jsonl_path(settings: dict, code: str) -> Path:
    base = Path(settings["jsonl_dir"])
    return (base if base.is_absolute() else BASE_DIR / base) / f"{code}.ikp.jsonl"


def run_ikp_pipeline(pdf_path: Path, settings: dict, document: dict, paths: dict) -> dict:
    settings = validate_ikp_settings(settings)
    name, code, kind = get_document_context(document)
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Selected legal PDF not found: {pdf_path}")
    sha = file_sha256(pdf_path)
    conn = get_connection(paths) if settings["save_to_db"] else None
    db = store_mod.Store(conn, settings["schema"], settings["table_prefix"]) if conn else None
    try:
        if db:
            db.ensure()
        # ---------------- 0. switches
        if not settings["enabled"]:
            latest = db.latest_version(code) if db else None
            if not latest:
                raise RuntimeError(f"{code}: ikp.enabled=false and no saved index exists")
            if latest[1] != sha:
                log(f"IKP | {code}: WARNING - latest saved index is for a different PDF file")
            rows = db.load_rows(code)
            log(f"IKP | {code}: disabled - using saved version {latest[0]} ({len(rows)} rows)")
            return _result(code, rows, latest[0], "SAVED", None, None, 0)
        version, resume = None, {}
        if db and settings["use_cache"]:
            done = db.find_version(code, sha, PARSER_VERSION, "COMPLETE")
            running = db.find_version(code, sha, PARSER_VERSION, "RUNNING")
            if done and not (running and running > done):
                latest = db.latest_version(code)
                if latest and latest[0] == done:
                    rows = db.load_rows(code)
                    say(f"IKP : {code} already indexed (version {done}) - reused from database, PDF not read again")
                    return _result(code, rows, done, "CACHE", None, None, 0)
            if running and (not done or running > done):
                version, resume = running, db.committed_chapters(code, running)
                say(f"IKP | {code}: resuming version {version} - {len(resume)} chapter(s) already saved")

        # ---------------- 1-2. read + Python split (deterministic, seconds)
        say(f"\nIKP | {name} ({code}) | {pdf_path.name} | sha {sha[:12]}")
        a = analyse(pdf_path, code, kind, name)
        log(f"READ            : {a.layout.page_count} pages | law as on: {a.law_as_on or 'not printed'}")
        if db and version is None:
            version = db.start_version(a, pdf_path.name, PARSER_VERSION)
            log(f"VERSION         : {version} (new)")
        version = version or 1

        # ---------------- 3. chapter by chapter
        llm_cfg = settings["llm"]
        prompt_base = (Path(paths["prompt_dir"]) / settings["prompt_file"]).read_text(encoding="utf-8")
        cache = db or store_mod.FileCache(_jsonl_path(settings, code).parent / "llm_cache")
        caller = _llm_caller(settings, paths)
        model_id = f"{llm_cfg['provider']}:{llm_cfg['model'] or 'default'}"
        regroup = lambda an, key: next(g for g in chapter_groups(an.parsed) if g.key == key)
        gate = lambda an, g: evaluate_group(an.parsed, g)
        groups = chapter_groups(a.parsed)
        chapter_log, llm_calls = [], 0
        log(f"CHAPTERS        : {len(groups)}")
        for gi, group_key in enumerate([g.key for g in groups], 1):
            group = regroup(a, group_key)
            q = gate(a, group)
            method, llm_info = "PYTHON", {}
            prior = resume.get(group_key)
            want_llm = llm_cfg["mode"] == "ALWAYS" or (llm_cfg["mode"] == "ON_FAIL" and q.status == "FAIL")
            if llm_cfg["chapters"]:  # restrict the LLM to the listed chapters
                want_llm = group_key in llm_cfg["chapters"] and llm_cfg["mode"] != "OFF"
            if prior and prior.get("method") == "LLM_ASSIST":
                want_llm = True  # replay from cache so keys stay identical to what was committed
            if want_llm and group.level != "FRONT":
                new_group, rep, q2 = llm_assist.assist_group(
                    a, group, prompt_base, caller, gate, regroup, re_enrich,
                    cache.cache_get, lambda k, r: cache.cache_put(k, model_id, r), model_id, q.issues)
                llm_calls += rep.calls
                llm_info = {"called": rep.called, "cached": rep.cached, "accepted_tags": rep.accepted,
                            "rejected_tags": rep.rejected[:50], "doubts": rep.doubts[:50], "error": rep.error,
                            "agreement": rep.agreement, "gate_before": rep.gate_before, "gate_after": rep.gate_after}
                if new_group is not group:
                    group, q, method = new_group, q2, "LLM_ASSIST"
            status_line = f"[{gi:>2}/{len(groups)}] {group.key:<22} {q.status:<4} {method:<10} units {q.metrics['units']:>3} nodes {q.metrics['nodes']:>4}"
            if prior:
                log(status_line + "  (already saved - skipped)")
            else:
                rows, texts = group_records(a, group)
                if settings["dry_run_fetch"]:
                    empty_keys = _dry_run_fetch(a, texts)
                    if empty_keys:
                        msg = f"dry-run: {len(empty_keys)} node(s) have empty own_text: {empty_keys[:5]}"
                        q.issues.append(msg)
                        if q.status == "PASS":
                            q.status = "WARN"
                if db:
                    db.commit_chapter(code, version, group.key, group.ord, group.label, rows, texts,
                                      {"status": q.status, "metrics": q.metrics, "issues": q.issues, "review": q.review},
                                      method, llm_info)
                log(status_line + ("  saved" if db else "") + (f"  | {'; '.join(q.issues)}" if q.issues else ""))
                if llm_info.get("error"):
                    log(f"                  LLM: {llm_info['error']}")
            chapter_log.append({"ord": group.ord, "chapter": group.key, "label": group.label, "status": q.status,
                                "method": method, "nodes": len(group.keys), "issues": q.issues,
                                "llm": json.dumps(llm_info)[:500] if llm_info else ""})
            if settings["stop_after_chapters"] and gi >= settings["stop_after_chapters"] and not prior:
                log("STOPPED         : stop_after_chapters reached (test switch) - run again to resume")
                return _result(code, [], version, "STOPPED", None, None, llm_calls)

        # ---------------- 4. document gate + publish
        dq = document_quality(a)
        failed_chapters = [c["chapter"] for c in chapter_log if c["status"] == "FAIL"]
        doc_status = "FAIL" if (dq.status == "FAIL" or failed_chapters) else dq.status
        doc_rows, doc_texts = document_records(a, dq)
        all_rows, all_texts = list(doc_rows[:2]), list(doc_texts)
        for g in chapter_groups(a.parsed):
            r, t = group_records(a, g)
            all_rows += r
            all_texts += t
        all_rows.append(doc_rows[2])
        quality = {"status": doc_status, "metrics": dq.metrics, "issues": dq.issues + [f"chapter {c} FAILED" for c in failed_chapters],
                   "review": dq.review}
        publish = not (doc_status == "FAIL" and settings["fail_on_gate"])
        if db:
            db.commit_chapter(code, version, "_DOC", 0, "document", [doc_rows[0], doc_rows[1], doc_rows[2]], doc_texts,
                              quality, "PYTHON", {})
            db.finish_version(code, version, "COMPLETE" if publish else "FAILED", quality, make_latest=publish)
        _print_quality(quality)
        manifest = {"doc": code, "version_no": version, "doc_type": kind, "name": name, "pdf_sha256": sha,
                    "parser": PARSER_VERSION, "law_as_on": a.law_as_on, "law_as_on_date": a.law_as_on_iso,
                    "indexed_at": now_iso(), "is_latest": publish, "quality": quality, "chapters": chapter_log}
        jsonl = write_jsonl(_jsonl_path(settings, code), manifest, all_texts) if settings["export_jsonl"] else None
        excel = None
        if settings["export_excel"]:
            try:
                excel = store_mod.export_excel(Path(paths["output_dir"]) / settings["excel_file"],
                                               str(document.get("document_key", code)), version, all_rows, all_texts,
                                               chapter_log, quality)
            except Exception as exc:
                say(f"EXCEL           : not written ({type(exc).__name__}: {exc})")
        say(f"VERSION {version}       : {'COMPLETE, latest' if publish else 'FAILED - not published as latest'}"
              f" | LLM calls {llm_calls}" + (f" | JSONL {jsonl.name}" if jsonl else "") + (f" | EXCEL {Path(excel).name}" if excel else ""))
        if not publish:
            raise RuntimeError(f"{code}: IKP quality gate FAILED: {quality['issues']}")
        return _result(code, all_rows, version, "INDEXED", excel, jsonl, llm_calls, quality)
    finally:
        if conn is not None:
            conn.close()


def _dry_run_fetch(a, texts: List[dict]) -> List[str]:
    """Return keys of operative nodes whose own_text is empty (gate §3.5).

    Checks the in-memory parsed document — no PDF re-open needed.
    FRONT / DOCUMENT / SCHEDULE root nodes are skipped (their own text is their heading line).
    """
    SKIP_LEVELS = {"DOCUMENT", "FRONT", "CHAPTER", "SCHEDULE", "PART", "ANNEX"}
    failed = []
    for rec in texts:
        if rec.get("level") in SKIP_LEVELS:
            continue
        if rec.get("role") in ("NON_OPERATIVE", "HISTORICAL"):
            continue
        own = (rec.get("own_text") or "").strip()
        import re as _re
        plain = _re.sub(r"\[\[PAGE \d+\]\]\n?", "", own).strip()
        if not plain:
            failed.append(rec["element"])
    return failed


def _result(code, rows, version, source, excel, jsonl, llm_calls, quality=None) -> dict:
    return {"document_code": code, "ikp_rows": len(rows), "rows": rows, "version_no": version, "source": source,
            "excel_path": excel, "jsonl_path": jsonl, "llm_calls": llm_calls, "quality": quality}


def _print_quality(q: dict) -> None:
    m = q["metrics"]
    say(f"QUALITY         : {q['status']} | chapters {m['chapters']} | schedules {m['schedules']} | units {m['units']} "
          f"({m['first_unit']}..{m['last_unit']}) | nodes {m['nodes']} | text coverage {m['text_coverage_pct']}% | "
          f"xrefs {m['xrefs']} ({m['xrefs_internal_resolved_pct']}% internal resolved) | definitions {m['definitions']}")
    for issue in q["issues"]:
        say(f"  ISSUE: {issue}")
    if q["review"]:
        log(f"  REVIEW ({len(q['review'])}): {', '.join(q['review'][:8])}{' ...' if len(q['review']) > 8 else ''}")


def run_standalone_ikp() -> None:
    import kp_settings
    from main import main_ikp
    main_ikp(kp_settings.load())


if __name__ == "__main__":
    run_standalone_ikp()
