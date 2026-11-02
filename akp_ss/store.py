"""AKP / PostgreSQL.

akp            same table and columns as before, plus:
                        akp_version  1, 2, 3 ... per source MKP (a re-run saves the next version)
                        is_latest    true only for the newest version (older rows kept, validation_status SUPERSEDED,
                                     so GKP - which reads validation_status='APPROVED' - always sees the newest)
                        inserted_at  when the row was saved
                        mkp_version  the MKP version it was split from;  tier  SIMPLE / COMPLEX
akp_cache      candidates + Standard + comparison per MKP and input hash (as before)
akp_llm_cache  raw model answers (a crash never pays twice)
akp_time_fact      amendment MKPs as time facts on the provision they amend
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

from psycopg2 import sql

from transport import get_field

from .parse import COLS


def _vec(v) -> Optional[str]:
    return "[" + ",".join(str(float(x)) for x in v) + "]" if v else None


class AkpStore:
    def __init__(self, conn, schema: str, table: str = "akp", cache_table: str = "akp_cache",
                 mkp_table: str = "mkp"):
        self.conn, self.schema = conn, schema
        self.table, self.cache_table, self.mkp_table = table, cache_table, mkp_table
        self.llm_table, self.time_table = f"{table}_llm_cache", f"{table.rsplit('_akp', 1)[0]}_time_fact"

    def t(self, name: str):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def run(self, q, params=None, fetch=False):
        with self.conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall() if fetch else None

    def ensure(self) -> None:
        T = self.t(self.table)
        stmts = [
            sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"),
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                instrument text NOT NULL, version_id text NOT NULL, source_mkp text NOT NULL, mkp_id text,
                source_hash text NOT NULL, ordinal integer NOT NULL, mkp text NOT NULL, type text NOT NULL,
                element text NOT NULL, concept text NOT NULL DEFAULT '', results text NOT NULL,
                concept_embedding vector, atom_embedding vector,
                validation_status text NOT NULL DEFAULT 'APPROVED', created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, source_mkp, ordinal))""").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_id text").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS akp_version integer NOT NULL DEFAULT 1").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS is_latest boolean NOT NULL DEFAULT true").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS inserted_at timestamptz NOT NULL DEFAULT now()").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_version integer").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS tier text").format(T),
            # SAME: akp_id of every atom; a SAME atom has empty RESULTS and the existing atom's id in same_akp_id
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS akp_id text").format(T),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS same_akp_id text").format(T),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
                source_mkp text NOT NULL, input_sha256 text NOT NULL, results_json jsonb NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, source_mkp, input_sha256))""").format(self.t(self.cache_table)),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (cache_key text PRIMARY KEY, model text, response text,
                inserted_at timestamptz NOT NULL DEFAULT now())""").format(self.t(self.llm_table)),
            sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
                amendment_mkp text NOT NULL, time_fact_id text NOT NULL, target_citation_key text NOT NULL,
                kind text, amended_by text, instrument_date date, effective_from date, replaced_text text, note text,
                inserted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, amendment_mkp))""").format(self.t(self.time_table)),
            # MKP table: versions (MKP adds these too; harmless if present)
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS is_latest boolean NOT NULL DEFAULT true").format(self.t(self.mkp_table)),
            sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS mkp_version integer NOT NULL DEFAULT 1").format(self.t(self.mkp_table)),
        ]
        existing = {(t, c) for t, c in self.run(sql.SQL(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema=%s"), (self.schema,), fetch=True)}
        for s in stmts:
            text = s.as_string(self.conn)
            m = re.search(r'ALTER TABLE "[^"]+"\."([^"]+)" ADD COLUMN IF NOT EXISTS (\w+)', text)
            if m and (m.group(1), m.group(2)) in existing:
                continue                     # no ALTER (and no table lock) when the column is already there
            self.run(s)
        # primary key must include akp_version so versions can live side by side
        rows = self.run(sql.SQL("""SELECT c.conname, array_agg(a.attname::text) FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum = ANY(c.conkey)
            WHERE c.conrelid = %s::regclass AND c.contype='p' GROUP BY c.conname"""),
            (f'"{self.schema}"."{self.table}"',), fetch=True)
        if rows and "akp_version" not in rows[0][1]:
            self.run(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(T, sql.Identifier(rows[0][0])))
            self.run(sql.SQL("ALTER TABLE {} ADD PRIMARY KEY (instrument, version_id, source_mkp, akp_version, ordinal)").format(T))
        self.run(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (instrument, version_id, source_mkp) WHERE is_latest").format(
            sql.Identifier(f"{self.table}_latest_idx"), T))
        self.run(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (akp_id) WHERE is_latest").format(
            sql.Identifier(f"{self.table}_akp_id_idx"), T))
        self.conn.commit()

    # ---------------- caches
    def llm_get(self, key: str) -> Optional[str]:
        r = self.run(sql.SQL("SELECT response FROM {} WHERE cache_key=%s").format(self.t(self.llm_table)), (key,), fetch=True)
        return r[0][0] if r else None

    def llm_put(self, key: str, model: str, response: str) -> None:
        self.run(sql.SQL("INSERT INTO {} (cache_key, model, response) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING")
                 .format(self.t(self.llm_table)), (key, model, response))
        self.conn.commit()

    def cache_get(self, instrument, version, source_mkp, fingerprint) -> Optional[dict]:
        r = self.run(sql.SQL("SELECT results_json FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s "
                             "AND input_sha256=%s").format(self.t(self.cache_table)),
                     (instrument, version, source_mkp, fingerprint), fetch=True)
        return r[0][0] if r else None

    def cache_put(self, instrument, version, source_mkp, fingerprint, data: dict, commit: bool = True) -> None:
        self.run(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, input_sha256, results_json)
            VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT (instrument, version_id, source_mkp, input_sha256)
            DO UPDATE SET results_json=EXCLUDED.results_json, updated_at=now()""").format(self.t(self.cache_table)),
                 (instrument, version, source_mkp, fingerprint, json.dumps(data, ensure_ascii=False, default=str)))
        if commit:
            self.conn.commit()

    # ---------------- reads
    def latest(self, instrument, version, source_mkp) -> Optional[dict]:
        r = self.run(sql.SQL("SELECT akp_version, source_hash, max(mkp_version), "
                             "count(*) FILTER (WHERE type IN ('A','A_REF')) FROM {} WHERE instrument=%s AND version_id=%s "
                             "AND source_mkp=%s AND is_latest AND validation_status='APPROVED' GROUP BY akp_version, source_hash "
                             "ORDER BY akp_version DESC LIMIT 1").format(self.t(self.table)),
                     (instrument, version, source_mkp), fetch=True)
        return {"akp_version": r[0][0], "source_hash": r[0][1], "mkp_version": r[0][2], "atoms": int(r[0][3] or 0)} if r else None

    def same_as_latest(self, cur, instrument, version, source_mkp, rows: List[dict]) -> Optional[int]:
        """The latest approved akp_version when `rows` say exactly the same (type, element, results), else None."""
        cur.execute(sql.SQL("SELECT akp_version, type, element, results FROM {} WHERE instrument=%s AND version_id=%s "
                            "AND source_mkp=%s AND is_latest AND validation_status='APPROVED' ORDER BY ordinal")
                    .format(self.t(self.table)), (instrument, version, source_mkp))
        old = cur.fetchall()
        if not old:
            return None
        norm = lambda s: " ".join(str(s or "").split())
        a = sorted((k, e, norm(r)) for _, k, e, r in old)
        b = sorted((r["TYPE"], r["ELEMENT"], norm(r.get("RESULTS"))) for r in rows)
        return int(old[0][0]) if a == b else None

    def touch_mkp_version(self, cur, instrument, version, source_mkp, mkp_version, rows=None, vectors=None) -> None:
        """Same AKP again: no new AKP version. Record the MKP version it was made from, and refresh the atom
        embeddings (they are search data, not content - e.g. after a change of embedding model)."""
        cur.execute(sql.SQL("UPDATE {} SET mkp_version=%s WHERE instrument=%s AND version_id=%s AND source_mkp=%s "
                            "AND is_latest").format(self.t(self.table)), (mkp_version, instrument, version, source_mkp))
        for i, row in enumerate(rows or []):
            cv, av = (vectors or {}).get(i, (None, None)) if row["TYPE"] == "A" else (None, None)
            if cv or av:
                cur.execute(sql.SQL("UPDATE {} SET concept_embedding=COALESCE(%s::vector, concept_embedding), "
                                    "atom_embedding=COALESCE(%s::vector, atom_embedding) WHERE instrument=%s AND version_id=%s "
                                    "AND source_mkp=%s AND element=%s AND type='A' AND is_latest").format(self.t(self.table)),
                            (_vec(cv), _vec(av), instrument, version, source_mkp, row["ELEMENT"]))

    def mkp_id(self, version, source_mkp) -> Optional[tuple]:
        r = self.run(sql.SQL("SELECT mkp_id, mkp_version FROM {} WHERE type='MKP' AND version_id=%s AND element=%s AND is_latest "
                             "AND mkp_id IS NOT NULL AND BTRIM(mkp_id) <> '' ORDER BY mkp_version DESC LIMIT 1")
                     .format(self.t(self.mkp_table)), (version, source_mkp), fetch=True)
        return r[0] if r else None

    def same_candidates(self, source_mkp: str, rows: List[dict], vectors: Dict[int, tuple], limit: int = 5,
                        min_similarity: float = 0.90) -> List[dict]:
        """Existing canonical atoms (latest, approved, TYPE A) with cosine similarity > min_similarity."""
        found = []
        for i, row in enumerate(rows):
            if row["TYPE"] != "A" or i not in vectors or not vectors[i][1]:
                continue
            v = vectors[i][1]
            try:
                matches = self.run(sql.SQL("""SELECT a.element, a.concept, a.results, a.instrument, a.version_id, a.source_mkp,
                    COALESCE(a.akp_id, a.source_mkp || '::' || a.element), 1 - (a.atom_embedding <=> %s::vector),
                    (SELECT m.results FROM {} m WHERE m.type='MKP' AND m.element=a.source_mkp AND m.version_id=a.version_id
                     AND COALESCE(m.is_latest, true) LIMIT 1)
                    FROM {} a WHERE a.type='A' AND a.validation_status='APPROVED' AND a.is_latest
                    AND a.source_mkp<>%s AND a.atom_embedding IS NOT NULL AND vector_dims(a.atom_embedding)=%s
                    ORDER BY a.atom_embedding <=> %s::vector LIMIT %s""").format(self.t(self.mkp_table), self.t(self.table)),
                                   (_vec(v), source_mkp, len(v), _vec(v), limit), fetch=True)
            except Exception:
                self.conn.rollback()
                matches = []
            cands = []
            for el, concept, results, inst, ver, src, aid, score, mkp_results in matches:
                if score is None or float(score) <= min_similarity:
                    continue
                cands.append({"akp_id": aid, "element": el, "concept": concept, "results": results, "instrument": inst,
                              "version": ver, "source_mkp": src,
                              "source_text": get_field({"RESULTS": mkp_results or ""}, "text"),
                              "similarity": round(float(score), 6)})
            found.append({"element": row["ELEMENT"], "concept": row["CONCEPT"], "new_results": row["RESULTS"],
                          "candidates": cands})
        return found

    # ---------------- save one MKP as the next AKP version (caller commits)
    def save_version(self, cur, instrument, version, source_mkp, mkp_id, mkp_version, fingerprint, rows: List[dict],
                     vectors: Dict[int, tuple], tier: str) -> int:
        T = self.t(self.table)
        cur.execute(sql.SQL("SELECT COALESCE(MAX(akp_version), 0) FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s")
                    .format(T), (instrument, version, source_mkp))
        akp_version = cur.fetchone()[0] + 1
        cur.execute(sql.SQL("""UPDATE {} SET is_latest=false,
            validation_status = CASE WHEN validation_status='APPROVED' THEN 'SUPERSEDED' ELSE validation_status END
            WHERE instrument=%s AND version_id=%s AND source_mkp=%s AND is_latest""").format(T),
                    (instrument, version, source_mkp))
        for i, row in enumerate(rows):
            cv, av = vectors.get(i, (None, None)) if row["TYPE"] == "A" else (None, None)
            cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, mkp_id, source_hash, ordinal, mkp, type,
                element, concept, results, concept_embedding, atom_embedding, validation_status, akp_version, is_latest,
                inserted_at, mkp_version, tier, akp_id, same_akp_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,
                %s::vector,'APPROVED',%s,true,now(),%s,%s,%s,%s)""").format(T),
                        (instrument, version, source_mkp, mkp_id, fingerprint, i, row["MKP"], row["TYPE"], row["ELEMENT"],
                         row["CONCEPT"], row["RESULTS"], _vec(cv), _vec(av), akp_version, mkp_version, tier,
                         f"{source_mkp}::{row['ELEMENT']}" if row["TYPE"] in ("A", "A_REF") else None,
                         row.get("SAME_AKP_ID")))
        return akp_version

    def save_time_fact(self, cur, instrument, version, source_mkp, tf: dict) -> None:
        cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, amendment_mkp, time_fact_id, target_citation_key, kind,
            amended_by, instrument_date, effective_from, replaced_text, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (instrument, version_id, amendment_mkp) DO UPDATE SET target_citation_key=EXCLUDED.target_citation_key,
            kind=EXCLUDED.kind, amended_by=EXCLUDED.amended_by, instrument_date=EXCLUDED.instrument_date,
            effective_from=EXCLUDED.effective_from, replaced_text=EXCLUDED.replaced_text, note=EXCLUDED.note,
            inserted_at=now()""").format(self.t(self.time_table)),
                    (instrument, version, source_mkp, tf["time_fact_id"], tf["target"], tf["kind"], tf["amended_by"],
                     tf["instrument_date"], tf["effective_from"], tf["replaced_text"], tf["note"]))

    # ---------------- embeddings left empty earlier (Vertex down) -> filled now
    def backfill(self, instrument: str, embed: Callable, limit: int = 2000) -> int:
        rows = self.run(sql.SQL("""SELECT ctid::text, concept, results FROM {} WHERE instrument=%s AND type='A' AND is_latest
            AND (atom_embedding IS NULL OR concept_embedding IS NULL) LIMIT %s""").format(self.t(self.table)),
                        (instrument, limit), fetch=True)
        done = 0
        for ctid, concept, results in rows:
            r = {"RESULTS": results}
            try:
                cv = embed(f"{concept} - {get_field(r, 'family')}")
                av = embed(get_field(r, "statement"))
            except Exception:
                break
            if not cv or not av:
                break
            self.run(sql.SQL("UPDATE {} SET concept_embedding=%s::vector, atom_embedding=%s::vector WHERE ctid=%s::tid")
                     .format(self.t(self.table)), (_vec(cv), _vec(av), ctid))
            done += 1
        self.conn.commit()
        return done

    # ---------------- for Excel
    def export_rows(self):
        std = self.run(sql.SQL("""SELECT instrument, source_mkp, mkp_id, mkp, type, element, concept, results, akp_version,
            inserted_at, akp_id, same_akp_id FROM {} WHERE is_latest AND validation_status='APPROVED' ORDER BY instrument, source_mkp, ordinal""")
                       .format(self.t(self.table)), fetch=True)
        cache = self.run(sql.SQL("""SELECT DISTINCT ON (instrument, source_mkp) instrument, source_mkp, results_json FROM {}
            ORDER BY instrument, source_mkp, updated_at DESC""").format(self.t(self.cache_table)), fetch=True)
        times = self.run(sql.SQL("""SELECT instrument, amendment_mkp, target_citation_key, kind, amended_by, instrument_date, effective_from,
            replaced_text, note
            FROM {} ORDER BY instrument, target_citation_key""").format(self.t(self.time_table)), fetch=True)
        return std, cache, times


def rows_from_db(records) -> List[dict]:
    return [dict(zip(COLS, r)) for r in records]
