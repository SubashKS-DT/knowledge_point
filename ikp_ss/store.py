"""IKP storage - versioned PostgreSQL tables, chapter checkpoints, LLM cache, Excel.

Tables (prefix = ikp.table_prefix, default 'ikp'):
  <p>_versions   one row per (doc, version_no): pdf sha, law_as_on, status RUNNING/COMPLETE/FAILED, is_latest
  <p>_chapters   one row per chapter committed in a version (the checkpoint used for resume)
  <p>            five-column IKP rows + version_no, chapter, indexed_at, is_latest
  <p>_nodes      one row per node: exact text, blocks (page + bbox + PDF line ids), anchors, amendments ...
  <p>_llm_cache  raw LLM responses by prompt hash
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

COLUMNS = ("DOC", "TYPE", "ELEMENT", "PAGE_NUMBER", "RESULTS")
EXCEL_CELL_LIMIT = 32000
NODE_COLS = ("doc", "version_no", "chapter", "element", "level", "number", "heading", "parent", "children", "role",
             "status", "path", "ord", "page_start", "page_end", "start_char", "end_char",
             "blocks", "start_anchor", "end_anchor", "own_text",
             "full_text", "footnotes", "amendments", "xrefs", "flags", "text_sha256",
             "run_id", "source_version", "qc_status")
JSON_COLS = {"children", "blocks", "footnotes", "amendments", "xrefs", "flags"}
# New common columns required by CLAUDE.md §2.4; added with ALTER TABLE in ensure()
_NEW_NODE_COLS = [
    ("start_char", "INTEGER"),
    ("end_char", "INTEGER"),
    ("run_id", "TEXT"),
    ("source_version", "TEXT"),
    ("qc_status", "TEXT"),
]


def _node_value(rec: dict, col: str, version: int):
    """Extract the value for one NODE_COLS column from a text record."""
    if col == "version_no":
        return version
    if col in JSON_COLS:
        return json.dumps(rec.get(col), ensure_ascii=False, default=str)
    return rec.get(col)


class Store:
    def __init__(self, conn, schema: str = "public", prefix: str = "ikp"):
        self.conn, self.schema, self.prefix = conn, schema, prefix

    def t(self, name: str):
        from psycopg2 import sql
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(self.prefix if name == "index" else f"{self.prefix}_{name}"))

    def _exec(self, query, params=None, fetch=False):
        from psycopg2 import sql
        with self.conn.cursor() as cur:
            cur.execute(query if not isinstance(query, str) else sql.SQL(query), params)
            return cur.fetchall() if fetch else None

    # ------------------------------------------------------------ schema
    def ensure(self) -> None:
        from psycopg2 import sql
        q = lambda text, *names: sql.SQL(text).format(*[self.t(n) for n in names])
        self._exec(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
        self._exec(q("""CREATE TABLE IF NOT EXISTS {} (
            doc text NOT NULL, version_no integer NOT NULL, pdf_sha256 text NOT NULL, source_file text,
            doc_name text, doc_type text, law_as_on text, law_as_on_date date, parser_version text,
            status text NOT NULL, is_latest boolean NOT NULL DEFAULT false, quality jsonb,
            started_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
            PRIMARY KEY (doc, version_no))""", "versions"))
        self._exec(q("""CREATE TABLE IF NOT EXISTS {} (
            doc text NOT NULL, version_no integer NOT NULL, chapter text NOT NULL, ord integer, label text,
            status text, method text, quality jsonb, llm jsonb, node_count integer,
            committed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (doc, version_no, chapter))""", "chapters"))
        self._exec(q("""CREATE TABLE IF NOT EXISTS {} (
            doc text NOT NULL, version_no integer NOT NULL, chapter text NOT NULL, type text, element text,
            page_number text, results text, indexed_at timestamptz NOT NULL DEFAULT now(),
            is_latest boolean NOT NULL DEFAULT false)""", "index"))
        self._exec(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (doc, version_no, chapter)").format(
            sql.Identifier(f"{self.prefix}_dvc"), self.t("index")))
        self._exec(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (doc, is_latest, element)").format(
            sql.Identifier(f"{self.prefix}_latest"), self.t("index")))
        self._exec(q("""CREATE TABLE IF NOT EXISTS {} (
            doc text NOT NULL, version_no integer NOT NULL, chapter text NOT NULL, element text NOT NULL,
            level text, number text, heading text, parent text, children jsonb, role text, status text, path text,
            ord integer, page_start integer, page_end integer, blocks jsonb, start_anchor text, end_anchor text,
            own_text text, full_text text, footnotes jsonb, amendments jsonb, xrefs jsonb, flags jsonb,
            text_sha256 text, indexed_at timestamptz NOT NULL DEFAULT now(), is_latest boolean NOT NULL DEFAULT false,
            PRIMARY KEY (doc, version_no, element))""", "nodes"))
        self._exec(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (doc, is_latest, element)").format(
            sql.Identifier(f"{self.prefix}_nodes_latest"), self.t("nodes")))
        self._exec(q("""CREATE TABLE IF NOT EXISTS {} (
            cache_key text PRIMARY KEY, model text, response text, created_at timestamptz NOT NULL DEFAULT now())""",
                     "llm_cache"))
        # Add new common columns (CLAUDE.md §2.4) to existing tables without breaking old rows
        for col_name, col_type in _NEW_NODE_COLS:
            self._exec(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                self.t("nodes"), sql.Identifier(col_name), sql.SQL(col_type)))
        self.conn.commit()

    # ------------------------------------------------------------ versions
    def find_version(self, doc: str, sha: str, parser: str, status: str) -> Optional[int]:
        from psycopg2 import sql
        rows = self._exec(sql.SQL("SELECT version_no FROM {} WHERE doc=%s AND pdf_sha256=%s AND parser_version=%s "
                                  "AND status=%s ORDER BY version_no DESC LIMIT 1").format(self.t("versions")),
                          (doc, sha, parser, status), fetch=True)
        return rows[0][0] if rows else None

    def latest_version(self, doc: str) -> Optional[tuple]:
        from psycopg2 import sql
        rows = self._exec(sql.SQL("SELECT version_no, pdf_sha256, parser_version FROM {} WHERE doc=%s AND is_latest")
                          .format(self.t("versions")), (doc,), fetch=True)
        return rows[0] if rows else None

    def start_version(self, a, source_file: str, parser: str) -> int:
        from psycopg2 import sql
        rows = self._exec(sql.SQL("SELECT COALESCE(MAX(version_no),0)+1 FROM {} WHERE doc=%s").format(self.t("versions")),
                          (a.code,), fetch=True)
        version = rows[0][0]
        self._exec(sql.SQL("""INSERT INTO {} (doc, version_no, pdf_sha256, source_file, doc_name, doc_type, law_as_on,
            law_as_on_date, parser_version, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING')""").format(self.t("versions")),
                   (a.code, version, a.pdf_sha256, source_file, a.name, a.doc_type, a.law_as_on,
                    a.law_as_on_iso or None, parser))
        self.conn.commit()
        return version

    def committed_chapters(self, doc: str, version: int) -> Dict[str, dict]:
        from psycopg2 import sql
        rows = self._exec(sql.SQL("SELECT chapter, status, method, llm FROM {} WHERE doc=%s AND version_no=%s")
                          .format(self.t("chapters")), (doc, version), fetch=True)
        return {r[0]: {"status": r[1], "method": r[2], "llm": r[3]} for r in rows}

    # ------------------------------------------------------------ chapter commit (checkpoint)
    def commit_chapter(self, doc: str, version: int, chapter: str, ord_: int, label: str, rows: List[dict],
                       texts: List[dict], quality: dict, method: str, llm: dict) -> None:
        from psycopg2 import sql
        from psycopg2.extras import execute_values
        try:
            with self.conn.cursor() as cur:
                for table in ("index", "nodes", "chapters"):
                    cur.execute(sql.SQL("DELETE FROM {} WHERE doc=%s AND version_no=%s AND chapter=%s").format(self.t(table)),
                                (doc, version, chapter))
                if rows:
                    execute_values(cur, sql.SQL("INSERT INTO {} (doc, version_no, chapter, type, element, page_number, results) VALUES %s")
                                   .format(self.t("index")).as_string(self.conn),
                                   [(doc, version, chapter, r["TYPE"], r["ELEMENT"], r["PAGE_NUMBER"], r["RESULTS"]) for r in rows],
                                   page_size=1000)
                if texts:
                    cols = sql.SQL(", ").join(sql.Identifier(c) for c in NODE_COLS)
                    execute_values(cur, sql.SQL("INSERT INTO {} ({}) VALUES %s").format(self.t("nodes"), cols).as_string(self.conn),
                                   [tuple(_node_value(t, c, version) for c in NODE_COLS)
                                    for t in texts], page_size=300)
                cur.execute(sql.SQL("""INSERT INTO {} (doc, version_no, chapter, ord, label, status, method, quality, llm, node_count)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""").format(self.t("chapters")),
                            (doc, version, chapter, ord_, label, quality.get("status"), method,
                             json.dumps(quality, default=str), json.dumps(llm, default=str), len(texts)))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def finish_version(self, doc: str, version: int, status: str, quality: dict, make_latest: bool) -> None:
        from psycopg2 import sql
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql.SQL("UPDATE {} SET status=%s, quality=%s, completed_at=now() WHERE doc=%s AND version_no=%s")
                            .format(self.t("versions")), (status, json.dumps(quality, default=str), doc, version))
                if make_latest:
                    for table in ("versions", "index", "nodes"):
                        cur.execute(sql.SQL("UPDATE {} SET is_latest = (version_no = %s) WHERE doc=%s").format(self.t(table)),
                                    (version, doc))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------ reading
    def load_rows(self, doc: str, version: Optional[int] = None) -> List[dict]:
        from psycopg2 import sql
        where, params = ("is_latest", (doc,)) if version is None else ("version_no=%s", (doc, version))
        rows = self._exec(sql.SQL("SELECT doc, type, element, page_number, results FROM {} WHERE doc=%s AND "
                                  + where + " ORDER BY ctid").format(self.t("index")), params, fetch=True)
        return [dict(zip(COLUMNS, r)) for r in rows]

    def load_nodes(self, doc: str, version: Optional[int] = None) -> List[dict]:
        from psycopg2 import sql
        where, params = ("is_latest", (doc,)) if version is None else ("version_no=%s", (doc, version))
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in NODE_COLS)
        rows = self._exec(sql.SQL("SELECT {} FROM {} WHERE doc=%s AND " + where + " ORDER BY ord").format(cols, self.t("nodes")),
                          params, fetch=True)
        return [dict(zip(NODE_COLS, r)) for r in rows]

    # ------------------------------------------------------------ llm cache
    def cache_get(self, key: str) -> Optional[str]:
        from psycopg2 import sql
        rows = self._exec(sql.SQL("SELECT response FROM {} WHERE cache_key=%s").format(self.t("llm_cache")), (key,), fetch=True)
        return rows[0][0] if rows else None

    def cache_put(self, key: str, model: str, response: str) -> None:
        from psycopg2 import sql
        self._exec(sql.SQL("INSERT INTO {} (cache_key, model, response) VALUES (%s,%s,%s) ON CONFLICT (cache_key) DO NOTHING")
                   .format(self.t("llm_cache")), (key, model, response))
        self.conn.commit()


class FileCache:
    """LLM cache when PostgreSQL is not used (save_to_db=false)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def cache_get(self, key: str) -> Optional[str]:
        p = self.folder / f"{key}.txt"
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def cache_put(self, key: str, model: str, response: str) -> None:
        (self.folder / f"{key}.txt").write_text(response, encoding="utf-8")


# ---------------------------------------------------------------- Excel

def _safe(v) -> str:
    text = "" if v is None else (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text[:EXCEL_CELL_LIMIT]


def export_excel(path: Path, document_key: str, version: int, rows: List[dict], texts: List[dict],
                 chapters: List[dict], quality: dict) -> Path:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = load_workbook(path) if path.exists() else Workbook()
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", document_key.lower())[:20]
    sheets = {
        f"{base}_ikp": (["VERSION", *COLUMNS], [[version] + [r.get(c, "") for c in COLUMNS] for r in rows],
                          {"A": 9, "B": 12, "C": 8, "D": 40, "E": 14, "F": 110}, {6}),
        f"{base}_nodes": (["VERSION", "CHAPTER", "ELEMENT", "LEVEL", "NUMBER", "HEADING", "PARENT", "ROLE", "STATUS",
                           "PAGES", "OWN_TEXT", "BLOCKS", "START_ANCHOR", "END_ANCHOR", "AMENDMENTS", "FLAGS"],
                          [[version, t["chapter"], t["element"], t["level"], t["number"], t["heading"], t["parent"],
                            t["role"], t["status"], f"{t['page_start']}-{t['page_end']}", t["own_text"],
                            "; ".join(f"p{b['page']} {b['bbox']}" for b in t["blocks"]), t["start_anchor"],
                            t["end_anchor"], " | ".join(f"fn{x['footnote']} {x['kind']} {x['instrument']} {x['effective']}".strip()
                                                       for x in t["amendments"]), ",".join(t["flags"])] for t in texts],
                          {"A": 9, "B": 16, "C": 34, "D": 13, "E": 7, "F": 28, "G": 28, "H": 13, "I": 11, "J": 9,
                           "K": 90, "L": 30, "M": 30, "N": 30, "O": 45, "P": 20}, {11}),
        f"{base}_chapters": (["VERSION", "ORD", "CHAPTER", "LABEL", "STATUS", "METHOD", "NODES", "ISSUES", "LLM"],
                             [[version, c["ord"], c["chapter"], c["label"], c["status"], c["method"], c["nodes"],
                               " | ".join(c["issues"]), c.get("llm", "")] for c in chapters],
                             {"A": 9, "B": 6, "C": 20, "D": 45, "E": 9, "F": 13, "G": 8, "H": 70, "I": 50}, {8}),
        f"{base}_quality": (["METRIC", "VALUE"], [["version", version], ["status", quality.get("status")]]
                            + [[k, v] for k, v in quality.get("metrics", {}).items()]
                            + [["issue", i] for i in quality.get("issues", [])]
                            + [["review", k] for k in quality.get("review", [])], {"A": 32, "B": 110}, {2}),
    }
    for name, (header, data, widths, wrap) in sheets.items():
        if name in wb.sheetnames:
            del wb[name]
        ws = wb.create_sheet(name)
        ws.append(header)
        for row in data:
            ws.append([_safe(v) for v in row])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="203864")
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        for r in ws.iter_rows(min_row=2):
            for cell in r:
                cell.alignment = Alignment(vertical="top", wrap_text=cell.column in wrap)
    if "Sheet" in wb.sheetnames and wb["Sheet"].max_row == 1 and len(wb.sheetnames) > 1:
        del wb["Sheet"]
    fd, tmp = tempfile.mkstemp(prefix="ikp_", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
