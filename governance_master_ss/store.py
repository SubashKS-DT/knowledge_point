"""GOVERNANCE MASTER / PostgreSQL: the governance masters in two tables.

governance_master           MAIN table - the CURRENT content of every governance master, one row per element, so it can be
                          filtered and read without opening YAML:
    tsg_id | tsg_name | tg_id | tg_name | section | step_no | step_id | element_id | element_type |
    text | logic | check_order | facts | source_mkp | mkp_id | akp_id | citation | included_as |
    attributes (jsonb, the element exactly as in rulebook.yaml) | pack_status | updated_at
  section       HEADER (one header row per governance master) | STEP | OUTCOME | PRODUCT | GROUP | CHECK | FACT |
                OVERRIDE | TRIGGER | PRECEDENT
  text          the plain words: statement, question, name or description
  logic         the rule / formula / members / trigger
  included_as   SPINE, MEMBER, or why a linked law came in (PRODUCT PD_x, REFERENCE ..., OVERRIDE ..., DEFINITION ...)
  The rows of one sub-group are replaced together when its governance master changes (its old content goes to history).

governance_master_history   one row per change of a governance master (insert only, never edited):
    tsg_id | version | changed_at | fingerprint | pack_status | laws (jsonb) | change (jsonb: added / changed /
    removed element ids) | payload (the rulebook.yaml text) | report (jsonb)
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from psycopg2 import sql

MAIN_COLS = ("tsg_id", "tsg_name", "tg_id", "tg_name", "section", "step_no", "step_id", "element_id", "element_type",
             "text", "logic", "check_order", "facts", "source_mkp", "mkp_id", "akp_id", "citation", "included_as",
             "attributes", "pack_status")


class GovernanceMasterStore:
    def __init__(self, conn, schema: str, table: str, history_table: str):
        self.conn, self.schema, self.table, self.history = conn, schema, table, history_table

    def t(self, name: Optional[str] = None):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name or self.table))

    def ensure(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                tsg_id text NOT NULL, tsg_name text, tg_id text, tg_name text, section text NOT NULL,
                step_no integer, step_id text, element_id text NOT NULL, element_type text, text text, logic text,
                check_order text, facts text, source_mkp text, mkp_id text, akp_id text, citation text,
                included_as text, attributes jsonb NOT NULL DEFAULT '{{}}', pack_status text,
                updated_at timestamptz NOT NULL DEFAULT now())""").format(self.t()))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (tsg_id, section)").format(
                sql.Identifier(f"{self.table}_tsg_idx"), self.t()))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (source_mkp)").format(
                sql.Identifier(f"{self.table}_mkp_idx"), self.t()))
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                tsg_id text NOT NULL, version integer NOT NULL, changed_at timestamptz NOT NULL DEFAULT now(),
                fingerprint text NOT NULL, pack_status text NOT NULL, tsg_name text, tg_id text, tg_name text,
                laws jsonb NOT NULL DEFAULT '[]', change jsonb NOT NULL DEFAULT '{{}}', payload text NOT NULL,
                report jsonb NOT NULL DEFAULT '{{}}', PRIMARY KEY (tsg_id, version))""").format(self.t(self.history)))
        self.conn.commit()

    def exists(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{self.schema}.{self.history}",))
            return cur.fetchone()[0] is not None

    def latest_all(self) -> Dict[str, dict]:
        """tsg -> its latest history row: version, changed_at, fingerprint, pack_status, laws, names."""
        if not self.exists():
            return {}
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT DISTINCT ON (tsg_id) tsg_id, version, changed_at, fingerprint, pack_status,
                                   laws, tsg_name, tg_id, tg_name FROM {} ORDER BY tsg_id, version DESC""")
                        .format(self.t(self.history)))
            rows = cur.fetchall()
        return {r[0]: {"version": r[1], "changed_at": r[2], "fingerprint": r[3], "pack_status": r[4],
                       "laws": list(r[5] or []), "tsg_name": r[6], "tg": r[7], "tg_name": r[8]} for r in rows}

    def current_rows(self, tsg: str) -> Dict[tuple, str]:
        """(section, element_id) -> attributes JSON text, for the change summary."""
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT section, element_id, attributes::text FROM {} WHERE tsg_id=%s").format(self.t()),
                        (tsg,))
            return {(r[0], r[1]): r[2] for r in cur.fetchall()}

    def save(self, tsg: str, rows: List[dict], history: dict) -> int:
        """Replace the sub-group's rows in the main table and add one history row, in one transaction."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"governance_master:{tsg}",))
                cur.execute(sql.SQL("SELECT COALESCE(max(version), 0) FROM {} WHERE tsg_id=%s").format(
                    self.t(self.history)), (tsg,))
                version = int(cur.fetchone()[0]) + 1
                cur.execute(sql.SQL("DELETE FROM {} WHERE tsg_id=%s").format(self.t()), (tsg,))
                cols = sql.SQL(", ").join(map(sql.Identifier, MAIN_COLS))
                marks = sql.SQL(", ").join(sql.Placeholder() for _ in MAIN_COLS)
                q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(self.t(), cols, marks)
                for r in rows:
                    cur.execute(q, tuple(json.dumps(r[c], ensure_ascii=False, default=str) if c == "attributes" else r.get(c)
                                         for c in MAIN_COLS))
                cur.execute(sql.SQL("""INSERT INTO {} (tsg_id, version, fingerprint, pack_status, tsg_name, tg_id,
                    tg_name, laws, change, payload, report) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)""")
                            .format(self.t(self.history)),
                            (tsg, version, history["fingerprint"], history["pack_status"], history["tsg_name"],
                             history["tg_id"], history["tg_name"], json.dumps(history["laws"]),
                             json.dumps(history["change"]), history["payload"],
                             json.dumps(history["report"], ensure_ascii=False, default=str)))
            self.conn.commit()
            return version
        except Exception:
            self.conn.rollback()
            raise
