"""GKP / PostgreSQL.

gkp            one row per (atom, subgroup) membership - versioned like MKP / AKP:
                        instrument, version_id, source_mkp, mkp_id, akp_id, akp_element, akp_type, concept,
                        tg_id, tg_name, tsg_id, tsg_name,
                        status APPROVED / REVIEW / UNCLASSIFIED, method (AGREE / JUDGE / SAME_INHERITED /
                        AMENDMENT_RULE / AMENDMENT_TARGET / NONE), model_1, model_2, reason, scores, sources,
                        taxonomy_version, input_sha256, gkp_version, is_latest, inserted_at
gkp_cache          per MKP + input hash (as before)      gkp_llm_cache   raw model answers
gkp_tg18_proposals  proposals for new TG18 subgroups (PENDING / APPROVED / REJECTED / COVERED)
gkp_coverage       anchor provisions whose atoms did not land in the subgroup that names them
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from psycopg2 import sql

COLS = ("instrument", "version_id", "source_mkp", "mkp_id", "akp_id", "akp_element", "akp_type", "concept", "tg_id",
        "tg_name", "tsg_id", "tsg_name", "status", "method", "concept_score", "atom_score", "sources",
        "model_1", "model_2", "reason", "taxonomy_version", "input_sha256")


class GkpStore:
    def __init__(self, conn, schema: str, akp_table: str = "akp", prefix: str = "gkp"):
        self.conn, self.schema, self.akp_table, self.p = conn, schema, akp_table, prefix
        self.fact_edges = akp_table.rsplit("_akp", 1)[0] + "_atom_fact"

    def t(self, name: str):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def run(self, q, params=None, fetch=False):
        with self.conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall() if fetch else None

    def ensure(self) -> None:
        G = self.t(self.p)
        # the AKP table may come from an older AKP (no akp_id / same_akp_id / is_latest yet)
        have = {c for (c,) in self.run(sql.SQL("SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                                               "AND table_name=%s"), (self.schema, self.akp_table), fetch=True)}
        if not have:
            raise RuntimeError(f"AKP table {self.akp_table} not found - run AKP first")
        for col, typ in (("akp_id", "text"), ("same_akp_id", "text"), ("is_latest", "boolean NOT NULL DEFAULT true")):
            if col not in have:
                self.run(sql.SQL("ALTER TABLE {} ADD COLUMN {} " + typ).format(self.t(self.akp_table), sql.Identifier(col)))
        self.run(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
            source_mkp text NOT NULL, mkp_id text NOT NULL DEFAULT '', akp_element text NOT NULL, akp_type text NOT NULL,
            concept text NOT NULL DEFAULT '', tsg_id text NOT NULL DEFAULT '', tg_id text NOT NULL DEFAULT '',
            status text NOT NULL, concept_score double precision, atom_score double precision, reason text NOT NULL DEFAULT '',
            input_sha256 text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (instrument, version_id, source_mkp, akp_element, tsg_id))""").format(G))
        existing = {c for (c,) in self.run(sql.SQL("SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                                                   "AND table_name=%s"), (self.schema, self.p), fetch=True)}
        for col, typ in (("akp_id", "text"), ("tg_name", "text NOT NULL DEFAULT ''"), ("tsg_name", "text NOT NULL DEFAULT ''"),
                         ("method", "text NOT NULL DEFAULT ''"), ("sources", "jsonb NOT NULL DEFAULT '[]'"),
                         ("model_1", "text"), ("model_2", "text"), ("taxonomy_version", "text"),
                         ("gkp_version", "integer NOT NULL DEFAULT 1"), ("is_latest", "boolean NOT NULL DEFAULT true"),
                         ("akp_version", "integer"),
                         ("inserted_at", "timestamptz NOT NULL DEFAULT now()")):
            if col not in existing:
                self.run(sql.SQL("ALTER TABLE {} ADD COLUMN {} " + typ).format(G, sql.Identifier(col)))
        pk = self.run(sql.SQL("""SELECT c.conname, array_agg(a.attname::text) FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum = ANY(c.conkey)
            WHERE c.conrelid = %s::regclass AND c.contype='p' GROUP BY c.conname"""), (f'"{self.schema}"."{self.p}"',), fetch=True)
        if pk and "gkp_version" not in pk[0][1]:
            self.run(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(G, sql.Identifier(pk[0][0])))
            self.run(sql.SQL("ALTER TABLE {} ADD PRIMARY KEY (instrument, version_id, source_mkp, akp_element, tsg_id, "
                             "gkp_version)").format(G))
        for name, cols in (("akp_idx", "(akp_id) WHERE is_latest"), ("sub_idx", "(tsg_id) WHERE is_latest"),
                           ("mkp_idx", "(instrument, version_id, source_mkp) WHERE is_latest")):
            self.run(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} " + cols).format(sql.Identifier(f"{self.p}_{name}"), G))
        self.run(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
            source_mkp text NOT NULL, input_sha256 text NOT NULL, results_json jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (instrument, version_id, source_mkp, input_sha256))""")
                 .format(self.t(f"{self.p}_cache")))
        self.run(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (cache_key text PRIMARY KEY, model text, response text,
            inserted_at timestamptz NOT NULL DEFAULT now())""").format(self.t(f"{self.p}_llm_cache")))
        self.run(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
            source_mkp text NOT NULL, akp_element text NOT NULL, proposal_id text NOT NULL, name text NOT NULL,
            definition text NOT NULL, include_json jsonb NOT NULL, exclude_json jsonb NOT NULL, evidence text NOT NULL,
            reason text NOT NULL, status text NOT NULL DEFAULT 'PENDING', input_sha256 text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (instrument, version_id, source_mkp, akp_element))""")
                 .format(self.t(f"{self.p}_tg18_proposals")))
        self.run(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS approved_tsg_id text").format(self.t(f"{self.p}_tg18_proposals")))
        self.run(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (instrument text NOT NULL, version_id text NOT NULL,
            tsg_id text NOT NULL, anchor text NOT NULL, atoms integer NOT NULL, approved integer NOT NULL,
            status text NOT NULL, inserted_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (instrument, version_id, tsg_id, anchor))""").format(self.t(f"{self.p}_coverage")))
        self.conn.commit()

    # ---------------- caches
    def llm_get(self, key):
        r = self.run(sql.SQL("SELECT response FROM {} WHERE cache_key=%s").format(self.t(f"{self.p}_llm_cache")), (key,), fetch=True)
        return r[0][0] if r else None

    def llm_put(self, key, model, response):
        self.run(sql.SQL("INSERT INTO {} (cache_key, model, response) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING")
                 .format(self.t(f"{self.p}_llm_cache")), (key, model, response))
        self.conn.commit()

    # ---------------- AKP input
    def mkps(self, instrument: str, version: str) -> Dict[str, List[dict]]:
        rows = self.run(sql.SQL("""SELECT source_mkp, mkp_id, ordinal, type, element, concept, results,
            concept_embedding::text, atom_embedding::text, COALESCE(akp_id, source_mkp || '::' || element), same_akp_id
            FROM {} WHERE instrument=%s AND version_id=%s AND validation_status='APPROVED' AND COALESCE(is_latest, true)
            ORDER BY source_mkp, ordinal""").format(self.t(self.akp_table)), (instrument, version), fetch=True)
        out: Dict[str, List[dict]] = {}
        for r in rows:
            out.setdefault(r[0], []).append(dict(zip(("source_mkp", "mkp_id", "ordinal", "type", "element", "concept", "results",
                                                      "concept_vector", "atom_vector", "akp_id", "same_akp_id"), r)))
        return out

    def canonical_atom(self, akp_id: str) -> Optional[dict]:
        r = self.run(sql.SQL("""SELECT concept, results, concept_embedding::text, atom_embedding::text, source_mkp, element
            FROM {} WHERE COALESCE(akp_id, source_mkp || '::' || element)=%s AND type='A' AND validation_status='APPROVED'
            AND COALESCE(is_latest, true) LIMIT 1""").format(self.t(self.akp_table)), (akp_id,), fetch=True)
        return dict(zip(("concept", "results", "concept_vector", "atom_vector", "source_mkp", "element"), r[0])) if r else None

    def atom_facts(self, instrument: str, version: str, source_mkp: str, element: str) -> List[str]:
        try:
            r = self.run(sql.SQL("SELECT fact_id FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s AND atom=%s "
                                 "AND is_latest").format(self.t(self.fact_edges)), (instrument, version, source_mkp, element), fetch=True)
            return [x[0] for x in r]
        except Exception:
            self.conn.rollback()
            return []

    # ---------------- memberships already decided (for inheritance and candidate sources)
    def approved_for_akp(self, akp_id: str) -> List[dict]:
        r = self.run(sql.SQL("""SELECT tg_id, tg_name, tsg_id, tsg_name, reason FROM {}
            WHERE akp_id=%s AND is_latest AND status='APPROVED' AND tsg_id<>''""").format(self.t(self.p)),
                     (akp_id,), fetch=True)
        return [dict(zip(("tg_id", "tg_name", "tsg_id", "tsg_name", "reason"), x)) for x in r]

    def approved_for_provision(self, prefix: str) -> List[dict]:
        r = self.run(sql.SQL("""SELECT DISTINCT tg_id, tg_name, tsg_id, tsg_name FROM {}
            WHERE is_latest AND status='APPROVED' AND tsg_id<>'' AND (source_mkp LIKE %s OR source_mkp LIKE %s)
            AND akp_type <> 'MKP'""").format(self.t(self.p)), (prefix + "#%", prefix + "\\_%"), fetch=True)
        return [dict(zip(("tg_id", "tg_name", "tsg_id", "tsg_name"), x)) for x in r]

    def subgroups_by_similar_atom(self, vec: str, instrument: str, k: int, min_similarity: float) -> List[tuple]:
        """TSGs of the nearest approved atoms of OTHER documents (meaning, not words): a notification's
        'consideration in the form of construction service' lands next to s.2(31) / s.7(1)(a) barter atoms.
        Returns (tsg_id, similarity, akp_id) for atoms with similarity >= min_similarity."""
        if not vec:
            return []
        try:
            return self.run(sql.SQL("""SELECT g.tsg_id, n.sim, n.akp_id FROM (
                    SELECT a.instrument, a.version_id, a.source_mkp, a.element,
                           COALESCE(a.akp_id, a.source_mkp || '::' || a.element) AS akp_id,
                           1 - (a.atom_embedding <=> %s::vector) AS sim
                    FROM {} a WHERE a.type='A' AND a.is_latest AND a.validation_status='APPROVED' AND a.instrument<>%s
                      AND a.atom_embedding IS NOT NULL AND vector_dims(a.atom_embedding) = vector_dims(%s::vector)
                    ORDER BY a.atom_embedding <=> %s::vector LIMIT %s) n
                JOIN {} g ON g.instrument=n.instrument AND g.version_id=n.version_id AND g.source_mkp=n.source_mkp
                    AND g.akp_element=n.element AND g.is_latest AND g.status='APPROVED' AND g.tsg_id<>''
                WHERE n.sim >= %s ORDER BY n.sim DESC""").format(self.t(self.akp_table), self.t(self.p)),
                            (vec, instrument, vec, vec, k, min_similarity), fetch=True)
        except Exception:
            self.conn.rollback()
            return []

    def subgroups_by_fact(self, fact_id: str, exclude_mkp: str, limit: int = 5) -> List[tuple]:
        try:
            return self.run(sql.SQL("""SELECT g.tsg_id, count(*) FROM {} e JOIN {} g ON g.instrument=e.instrument
                AND g.version_id=e.version_id AND g.source_mkp=e.source_mkp AND g.akp_element=e.atom
                WHERE e.fact_id=%s AND e.is_latest AND g.is_latest AND g.status='APPROVED' AND g.tsg_id<>''
                AND e.source_mkp<>%s GROUP BY 1 ORDER BY 2 DESC LIMIT %s""").format(self.t(self.fact_edges), self.t(self.p)),
                            (fact_id, exclude_mkp, limit), fetch=True)
        except Exception:
            self.conn.rollback()
            return []

    def latest_state(self, instrument, version, source_mkp) -> Optional[dict]:
        r = self.run(sql.SQL("""SELECT max(input_sha256), max(taxonomy_version), count(*) FILTER (WHERE status='REVIEW'),
            count(*) FILTER (WHERE status='UNCLASSIFIED'), max(gkp_version), count(*), max(akp_version), max(inserted_at)
            FROM {} WHERE instrument=%s AND version_id=%s AND source_mkp=%s AND is_latest""").format(self.t(self.p)),
                     (instrument, version, source_mkp), fetch=True)
        if not r or not r[0][5]:
            return None
        sha, tv, rev, unc, ver, n, akv, ts = r[0]
        return {"input_sha256": sha, "taxonomy_version": tv, "review": rev, "unclassified": unc, "gkp_version": ver,
                "akp_version": akv, "inserted_at": ts}

    def akp_state(self, instrument, version, source_mkp) -> Optional[dict]:
        """Latest approved AKP version of one MKP (and when it was saved)."""
        r = self.run(sql.SQL("SELECT max(akp_version), max(inserted_at) FROM {} WHERE instrument=%s AND version_id=%s "
                             "AND source_mkp=%s AND is_latest AND validation_status='APPROVED'").format(self.t(self.akp_table)),
                     (instrument, version, source_mkp), fetch=True)
        return {"akp_version": r[0][0], "inserted_at": r[0][1]} if r and r[0][0] is not None else None

    def same_as_latest(self, instrument, version, source_mkp, rows: List[dict]) -> Optional[int]:
        """Latest gkp_version when `rows` give exactly the same memberships, else None."""
        old = self.run(sql.SQL("SELECT gkp_version, akp_element, tsg_id, status FROM {} WHERE instrument=%s AND version_id=%s "
                               "AND source_mkp=%s AND is_latest").format(self.t(self.p)),
                       (instrument, version, source_mkp), fetch=True)
        if not old:
            return None
        a = sorted((e, s, st) for _, e, s, st in old)
        b = sorted((r["akp_element"], r.get("tsg_id") or "", r["status"]) for r in rows)
        return int(old[0][0]) if a == b else None

    def touch_akp_version(self, instrument, version, source_mkp, akp_version, taxonomy_version=None) -> None:
        """Same groups again: keep the version, note the AKP version (and taxonomy / method) it was checked with."""
        self.run(sql.SQL("UPDATE {} SET akp_version=%s, taxonomy_version=COALESCE(%s, taxonomy_version) WHERE instrument=%s "
                         "AND version_id=%s AND source_mkp=%s AND is_latest")
                 .format(self.t(self.p)), (akp_version, taxonomy_version, instrument, version, source_mkp))
        self.conn.commit()

    def tsg_counts(self, instrument, version, source_mkps: List[str]) -> dict:
        """{tsg_id: number of atoms APPROVED in it} for these MKPs (latest rows)."""
        rows = self.run(sql.SQL("SELECT tsg_id, tsg_name, count(DISTINCT source_mkp || '::' || akp_element) FROM {} "
                                "WHERE instrument=%s AND version_id=%s AND source_mkp = ANY(%s) AND is_latest "
                                "AND status='APPROVED' AND tsg_id <> '' GROUP BY tsg_id, tsg_name ORDER BY tsg_id")
                        .format(self.t(self.p)), (instrument, version, list(source_mkps)), fetch=True)
        return {t: {"name": n, "atoms": int(c)} for t, n, c in rows}

    # ---------------- save one MKP's memberships as the next version
    def save_version(self, instrument, version, source_mkp, rows: List[dict]) -> int:
        G = self.t(self.p)
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT COALESCE(MAX(gkp_version), 0) FROM {} WHERE instrument=%s AND version_id=%s "
                                    "AND source_mkp=%s").format(G), (instrument, version, source_mkp))
                ver = cur.fetchone()[0] + 1
                cur.execute(sql.SQL("UPDATE {} SET is_latest=false WHERE instrument=%s AND version_id=%s AND source_mkp=%s "
                                    "AND is_latest").format(G), (instrument, version, source_mkp))
                for r in rows:
                    vals = [r.get(c) for c in COLS]
                    vals[COLS.index("sources")] = json.dumps(r.get("sources") or [], ensure_ascii=False)
                    cur.execute(sql.SQL("INSERT INTO {} (" + ", ".join(COLS) + ", akp_version, gkp_version, is_latest, inserted_at) VALUES (" +
                                        ", ".join(["%s"] * len(COLS)) + ", %s, %s, true, now())").format(G),
                                vals + [r.get("akp_version"), ver])
            self.conn.commit()
            return ver
        except Exception:
            self.conn.rollback()
            raise

    # ---------------- TG18 proposals
    def save_proposals(self, instrument, version, mkp, fingerprint, proposals: List[dict]) -> None:
        for p in proposals:
            self.run(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, akp_element, proposal_id, name, definition,
                include_json, exclude_json, evidence, reason, status, input_sha256)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'PENDING',%s)
                ON CONFLICT (instrument, version_id, source_mkp, akp_element) DO UPDATE SET proposal_id=EXCLUDED.proposal_id,
                name=EXCLUDED.name, definition=EXCLUDED.definition, include_json=EXCLUDED.include_json,
                exclude_json=EXCLUDED.exclude_json, evidence=EXCLUDED.evidence, reason=EXCLUDED.reason, status='PENDING',
                input_sha256=EXCLUDED.input_sha256, updated_at=now()""").format(self.t(f"{self.p}_tg18_proposals")),
                     (instrument, version, mkp, p["element"], p["proposal_id"], p["name"], p["definition"],
                      json.dumps(p["include"]), json.dumps(p["exclude"]), p["evidence"], p["reason"], fingerprint))
        self.conn.commit()

    def mark_covered(self, instrument, version, mkp, rows):
        done = sorted({r["akp_element"] for r in rows if r["status"] == "APPROVED"})
        if done:
            self.run(sql.SQL("""UPDATE {} SET status='COVERED', updated_at=now() WHERE instrument=%s AND version_id=%s
                AND source_mkp=%s AND akp_element = ANY(%s) AND status='PENDING'""").format(self.t(f"{self.p}_tg18_proposals")),
                     (instrument, version, mkp, done))
            self.conn.commit()

    def proposal(self, proposal_id: str) -> Optional[dict]:
        r = self.run(sql.SQL("""SELECT proposal_id, name, definition, include_json, exclude_json, status FROM {}
            WHERE proposal_id=%s LIMIT 1""").format(self.t(f"{self.p}_tg18_proposals")), (proposal_id,), fetch=True)
        return dict(zip(("proposal_id", "name", "definition", "include", "exclude", "status"), r[0])) if r else None

    def set_proposal_status(self, proposal_id: str, status: str, tsg_id: Optional[str] = None) -> None:
        self.run(sql.SQL("UPDATE {} SET status=%s, approved_tsg_id=%s, updated_at=now() WHERE proposal_id=%s")
                 .format(self.t(f"{self.p}_tg18_proposals")), (status, tsg_id, proposal_id))
        self.conn.commit()

    # ---------------- coverage: anchor provisions -> did their atoms land in the subgroup?
    def coverage(self, instrument: str, version: str, subs: List[dict]) -> List[tuple]:
        out = []
        for s in subs:
            for a in s.get("anchors") or []:
                if not a.startswith(instrument):
                    continue
                r = self.run(sql.SQL("""SELECT count(DISTINCT source_mkp || akp_element),
                    count(DISTINCT source_mkp || akp_element) FILTER (WHERE tsg_id=%s AND status='APPROVED')
                    FROM {} WHERE instrument=%s AND version_id=%s AND is_latest AND akp_type <> 'MKP'
                    AND (source_mkp LIKE %s OR source_mkp LIKE %s)""").format(self.t(self.p)),
                             (s["tsg_id"], instrument, version, a + "#%", a + "\\_%"), fetch=True)[0]
                if r[0]:
                    out.append((instrument, version, s["tsg_id"], a, r[0], r[1], "OK" if r[1] else "GAP"))
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {} WHERE instrument=%s AND version_id=%s").format(self.t(f"{self.p}_coverage")),
                        (instrument, version))
            for row in out:
                cur.execute(sql.SQL("INSERT INTO {} (instrument, version_id, tsg_id, anchor, atoms, approved, status) "
                                    "VALUES (%s,%s,%s,%s,%s,%s,%s)").format(self.t(f"{self.p}_coverage")), row)
        self.conn.commit()
        return out

    # ---------------- Excel
    def export(self):
        g = self.run(sql.SQL("""SELECT instrument, version_id, source_mkp, mkp_id, akp_id, akp_element, akp_type, concept,
            tg_id, tg_name, tsg_id, tsg_name, status, method, concept_score, atom_score, reason, model_1, model_2,
            gkp_version, inserted_at FROM {} WHERE is_latest ORDER BY instrument, source_mkp, akp_element, tsg_id""")
                     .format(self.t(self.p)), fetch=True)
        p = self.run(sql.SQL("""SELECT instrument, version_id, source_mkp, akp_element, proposal_id, name, definition, evidence,
            reason, status, approved_tsg_id FROM {} ORDER BY instrument, source_mkp, akp_element""")
                     .format(self.t(f"{self.p}_tg18_proposals")), fetch=True)
        c = self.run(sql.SQL("SELECT instrument, version_id, tsg_id, anchor, atoms, approved, status, inserted_at FROM {} "
                             "ORDER BY status, instrument, tsg_id, anchor").format(self.t(f"{self.p}_coverage")), fetch=True)
        return g, p, c


def provision_prefix(source_mkp: str) -> str:
    """'CGSTA17_S7_SS1_Ca#AMD1' -> 'CGSTA17_S7_SS1_Ca'"""
    return source_mkp.split("#", 1)[0]


def unit_prefix(source_mkp: str) -> str:
    m = re.match(r"^([A-Z0-9]+_(?:S|R|SCH|PA|AR|EN|O)[0-9A-Z]*)", provision_prefix(source_mkp))
    return m.group(1) if m else provision_prefix(source_mkp)
