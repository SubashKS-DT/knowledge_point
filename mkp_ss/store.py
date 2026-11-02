"""MKP / PostgreSQL: the existing mkp table (same columns and row format AKP reads).

mkp            cluster | type | element | results | embedding vector | version_id | mkp_id
                      + mkp_version (1, 2, 3 ... per element) and is_latest: a re-run never deletes, it inserts the
                      next version and marks the earlier rows is_latest=false (CLAUDE.md §2.2)
mkp_cache      doc, version_id, scope_key, input_sha256 -> Gemini / Claude / Standard / audit rows  (unchanged)
mkp_llm_cache  one row per raw model answer, so a crash after the Gemini / Claude call does not pay again
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, List, Optional

from psycopg2 import sql

from transport import clean, find_row_type, get_field, mkp_bundles, row_element

TABLE = "mkp"

# Common columns required on every table (CLAUDE.md §2.4); added via ALTER TABLE ADD COLUMN IF NOT EXISTS.
_NEW_COMMON_COLS = [
    ("run_id",          "TEXT"),
    ("inserted_at",     "TIMESTAMPTZ NOT NULL DEFAULT now()"),
    ("source_version",  "TEXT"),
    ("qc_status",       "TEXT"),
    ("doc_type",        "TEXT"),
    ("instrument_code", "TEXT"),
    ("mkp_version",     "INTEGER NOT NULL DEFAULT 1"),
    ("is_latest",       "BOOLEAN NOT NULL DEFAULT true"),
]


class MkpStore:
    def __init__(self, conn, schema: str = "public", table: str = TABLE):
        self.conn, self.schema, self.table = conn, schema, table

    def t(self, name: Optional[str] = None):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name or self.table))

    def run(self, query, params=None, fetch=False):
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if fetch else None

    def ensure(self) -> None:
        q = [
            sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"),
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)),
            sql.SQL("CREATE TABLE IF NOT EXISTS {} (cluster text, type text, element text, results text, "
                    "embedding vector, version_id text, mkp_id text)").format(self.t()),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS version_id text").format(self.t()),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_id text").format(self.t()),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (doc text NOT NULL, version_id text NOT NULL, scope_key text NOT NULL,
                input_sha256 text NOT NULL, results_json jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(doc, version_id, scope_key, input_sha256))""").format(self.t(f"{self.table}_cache")),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (cache_key text PRIMARY KEY, model text, response text,
                created_at timestamptz NOT NULL DEFAULT now())""").format(self.t(f"{self.table}_llm_cache")),
        ]
        for s in q:
            self.run(s)
        for col, col_type in _NEW_COMMON_COLS:
            self.run(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                self.t(), sql.Identifier(col), sql.SQL(col_type)))
        self.run(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (version_id, element) WHERE is_latest").format(
            sql.Identifier(f"{self.table}_latest_element_idx"), self.t()))
        self.conn.commit()

    # ---------------- caches
    def llm_get(self, key: str) -> Optional[str]:
        rows = self.run(sql.SQL("SELECT response FROM {} WHERE cache_key=%s").format(self.t(f"{self.table}_llm_cache")),
                        (key,), fetch=True)
        return rows[0][0] if rows else None

    def llm_put(self, key: str, model: str, response: str) -> None:
        self.run(sql.SQL("INSERT INTO {} (cache_key, model, response) VALUES (%s,%s,%s) ON CONFLICT (cache_key) DO NOTHING")
                 .format(self.t(f"{self.table}_llm_cache")), (key, model, response))
        self.conn.commit()

    def scope_cache(self, doc, version, scope_key, input_sha) -> Optional[dict]:
        rows = self.run(sql.SQL("SELECT results_json FROM {} WHERE doc=%s AND version_id=%s AND scope_key=%s "
                                "AND input_sha256=%s").format(self.t(f"{self.table}_cache")),
                        (doc, version, scope_key, input_sha), fetch=True)
        return rows[0][0] if rows else None

    # ---------------- reads
    def registry_slice(self, unit_key: str, limit: int = 12) -> List[dict]:
        rows = self.run(sql.SQL("SELECT element, results, version_id FROM {} WHERE type='MKP' AND is_latest AND element LIKE %s "
                                "ORDER BY ctid DESC LIMIT %s").format(self.t()), (unit_key + "%", limit), fetch=True)
        out = []
        for element, results, version in rows:
            r = {"RESULTS": results}
            out.append({"key": element, "version": version, "citation_key": get_field(r, "citation_key"),
                        "part": get_field(r, "part"), "concept": get_field(r, "concept"), "text": get_field(r, "text")})
        return out

    def stored_mkps(self, version: str, elements: List[str]) -> dict:
        """Latest saved MKP of each element: text, parent, knowledge type, version and its phrases (SP)."""
        rows = self.run(sql.SQL("SELECT element, type, results, mkp_version FROM {} WHERE version_id=%s AND type IN ('MKP','SP') "
                                "AND is_latest AND element = ANY(%s) ORDER BY ctid")
                        .format(self.t()), (version, list(elements)), fetch=True)
        out: dict = {}
        for e, kind, r, ver in rows:
            rec = out.setdefault(e, {"text": "", "parent": "", "knowledge_type": "", "version": 0, "phrases": []})
            if kind == "MKP":
                f = {"RESULTS": r}
                rec.update(text=get_field(f, "text"), parent=get_field(f, "parent"),
                           knowledge_type=get_field(f, "knowledge_type").upper(), version=int(ver or 1))
            else:
                rec["phrases"].append(get_field({"RESULTS": r}, "text"))
        return {e: v for e, v in out.items() if v["text"] or v["knowledge_type"]}

    # ---------------- commit one unit (one transaction)
    def commit(self, doc: str, version: str, cluster: str, standard: List[dict], by_provider: dict, scope_key: str,
               input_sha: str, source_hash: str, document_hash: str, embed: Callable[[str], Optional[list]],
               run_id: str = "", source_version: str = "", doc_type: str = "",
               qc_status: str = "APPROVED") -> dict:
        elements = sorted({r["ELEMENT"] for r in standard if r["TYPE"] in ("MKP", "SP", "UNL", "UNC")})
        stats = {"rows": 0, "embedded": 0, "pending": 0}
        vectors = {}
        for r in standard:                       # embeddings first, outside the transaction
            if r["TYPE"] == "MKP":
                v = None
                try:
                    v = embed(get_field(r, "text"))
                except Exception as exc:
                    stats.setdefault("embed_error", f"{type(exc).__name__}: {str(exc)[:150]}")
                vectors[r["ELEMENT"]] = v
                stats["embedded" if v else "pending"] += 1
        new_sig = _signatures(standard)
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (doc, version))
                # what is saved now, per element (latest rows)
                cur.execute(sql.SQL("SELECT element, type, results FROM {} WHERE version_id=%s AND is_latest "
                                    "AND type IN ('MKP','SP') AND element = ANY(%s) ORDER BY ctid").format(self.t()),
                            (version, elements))
                old_rows = [{"TYPE": k, "ELEMENT": el, "RESULTS": r} for el, k, r in cur.fetchall()]
                old_sig = _signatures(old_rows)
                # a new version only for an element whose MKP text, knowledge type or phrases differ
                same = lambda el: el in old_sig and old_sig[el]["key"] == new_sig[el]["key"]
                changed = [el for el in elements if el in new_sig and not same(el)]
                stats["unchanged"] = [el for el in elements if el in new_sig and same(el)]
                stats["new"] = [el for el in changed if el not in old_sig]
                stats["changed"] = [{"element": el, "old": old_sig[el]["text"], "new": new_sig[el]["text"]}
                                    for el in changed if el in old_sig]
                stats["versions"] = {}
                if changed:
                    cur.execute(sql.SQL("SELECT element, max(mkp_version) FROM {} WHERE version_id=%s AND element = ANY(%s) "
                                        "GROUP BY element").format(self.t()), (version, changed))
                    prior = {e: int(v or 0) for e, v in cur.fetchall()}
                    cur.execute(sql.SQL("SELECT COALESCE(max(mkp_version), 0) FROM {} WHERE version_id=%s AND cluster=%s "
                                        "AND type IN ('BATCH','SUM')").format(self.t()), (version, cluster))
                    unit_prior = int(cur.fetchone()[0] or 0)
                    cur.execute(sql.SQL("UPDATE {} SET is_latest=false WHERE version_id=%s AND is_latest AND (element = ANY(%s) "
                                        "OR (cluster=%s AND type IN ('BATCH','SUM')))").format(self.t()),
                                (version, changed, cluster))
                    for r in standard:
                        kind, result, element = find_row_type(r), clean(r.get("RESULTS")), row_element(r)
                        if kind in ("MKP", "SP") and element not in changed:
                            continue                     # same as the saved version: keep that version
                        if kind == "BATCH":
                            result = re.sub(r";\s*(?:(?:source_hash|document_hash):\s*[a-f0-9]+|source_input:\s*[A-Za-z0-9_]+|"
                                            r"scope_key:\s*[A-Za-z0-9_]+)", "", result, flags=re.I)
                            result += (f"; source_hash: {source_hash}; document_hash: {document_hash}; "
                                       f"source_input: IKP_INDEX_V2; scope_key: {scope_key}")
                            r["RESULTS"] = result
                        mkp_id = mkp_bundles([r])[0]["mkp_id"] if kind == "MKP" else None
                        v = vectors.get(element) if kind == "MKP" else None
                        mver = (prior.get(element, 0) + 1) if element in elements else (unit_prior + 1)
                        if kind == "MKP":
                            stats["versions"][element] = mver
                        cur.execute(sql.SQL(
                            "INSERT INTO {} (cluster, type, element, results, embedding, version_id, mkp_id, "
                            "run_id, source_version, qc_status, doc_type, instrument_code, mkp_version, is_latest) "
                            "VALUES (%s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s, true)").format(self.t()),
                            (clean(r.get("CLUSTER")), kind, element, result,
                             ("[" + ",".join(str(float(x)) for x in v) + "]") if v else None, version, mkp_id,
                             run_id, source_version, qc_status, doc_type, doc, mver))
                        stats["rows"] += 1
                cur.execute(sql.SQL("""INSERT INTO {} (doc, version_id, scope_key, input_sha256, results_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT (doc, version_id, scope_key, input_sha256)
                    DO UPDATE SET results_json=EXCLUDED.results_json, created_at=now()""").format(self.t(f"{self.table}_cache")),
                            (doc, version, scope_key, input_sha, json.dumps(by_provider, ensure_ascii=False)))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return stats

    # ---------------- embeddings that failed earlier (Vertex down) are filled in on the next run
    def backfill_embeddings(self, doc: str, embed: Callable[[str], Optional[list]], limit: int = 2000) -> int:
        rows = self.run(sql.SQL("SELECT ctid::text, results FROM {} WHERE type='MKP' AND is_latest AND embedding IS NULL AND element LIKE %s "
                                "AND version_id <> 'LEGACY' LIMIT %s").format(self.t()), (doc + "%", limit), fetch=True)
        done = 0
        for ctid, results in rows:
            try:
                v = embed(get_field({"RESULTS": results}, "text"))
            except Exception:
                break
            if not v:
                break
            self.run(sql.SQL("UPDATE {} SET embedding=%s::vector WHERE ctid=%s::tid").format(self.t()),
                     ("[" + ",".join(str(float(x)) for x in v) + "]", ctid))
            done += 1
        self.conn.commit()
        return done


def sha(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _signatures(rows: List[dict]) -> dict:
    """element -> {text, key}; key = (MKP words, knowledge type, phrase words): what decides a real change."""
    acc: dict = {}
    for r in rows:
        kind, el = find_row_type(r), row_element(r)
        rec = acc.setdefault(el, {"text": "", "kt": "", "phrases": []})
        if kind == "MKP":
            rec["text"], rec["kt"] = get_field(r, "text"), get_field(r, "knowledge_type").upper()
        elif kind == "SP":
            rec["phrases"].append(_norm(get_field(r, "text")))
    return {el: {"text": v["text"], "key": (_norm(v["text"]), v["kt"], tuple(v["phrases"]))}
            for el, v in acc.items() if v["text"] or v["kt"]}
