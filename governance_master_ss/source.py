"""GOVERNANCE MASTER / source: everything GOVERNANCE MASTER reads, as one plain snapshot (from PostgreSQL, or a JSON file in tests).

Snapshot (all latest rows only):
  akp          akp rows: instrument, version_id, source_mkp, mkp_id, ordinal, type, element, concept,
               results, akp_id, same_akp_id
  facts        akp_fact: fact_id -> name, entity, data_type, unit, options, evidence, derived_from, nl,
               possible_duplicate_of
  gkp          gkp: source_mkp, akp_element, akp_type, tg_id, tsg_id, status
  time_facts   akp_time_fact: target_citation_key, kind, effective_from
  questions    mkp: MKP element (citation_key#part) -> its cluster's business question
  definitions  ikp DEF rows: term -> defined_in (IKP key)
The snapshot is saved next to every rulebook, so a rulebook can always be rebuilt from exactly what it read.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

from psycopg2 import sql

from transport import get_field, parse_results

AKP_COLS = ("instrument", "version_id", "source_mkp", "mkp_id", "ordinal", "type", "element", "concept", "results",
            "akp_id", "same_akp_id")


@dataclass
class Snapshot:
    akp: List[dict] = field(default_factory=list)
    facts: Dict[str, dict] = field(default_factory=dict)
    gkp: List[dict] = field(default_factory=list)
    time_facts: List[dict] = field(default_factory=list)
    questions: Dict[str, str] = field(default_factory=dict)
    definitions: List[dict] = field(default_factory=list)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Snapshot":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def _t(schema: str, name: str):
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _jl(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else [x]
    except (TypeError, ValueError):
        return [s.strip() for s in str(v).strip("[]").split(",") if s.strip()]


def from_db(conn, schema: str, tables: Dict[str, str], instruments: List[str]) -> Snapshot:
    """Read the latest IKP-GKP rows of the given instruments (all MKPs: links may point anywhere in them)."""
    s = Snapshot()
    akp = _rows(conn, sql.SQL("SELECT " + ", ".join(AKP_COLS) + " FROM {} WHERE is_latest AND instrument = ANY(%s) "
                              "AND COALESCE(validation_status, 'APPROVED') <> 'SUPERSEDED' "
                              "ORDER BY instrument, source_mkp, ordinal").format(_t(schema, tables["akp"])),
                (list(instruments),))
    s.akp = [dict(zip(AKP_COLS, r)) for r in akp]
    for r in _rows(conn, sql.SQL("SELECT fact_id, name, entity, data_type, unit, options, evidence, derived_from, nl, "
                                 "possible_duplicate_of FROM {}").format(_t(schema, tables["fact"]))):
        s.facts[r[0]] = {"name": r[1], "entity": r[2], "data_type": r[3], "unit": r[4], "options": _jl(r[5]),
                         "evidence": _jl(r[6]), "derived_from": _jl(r[7]), "nl": r[8], "possible_duplicate_of": r[9]}
    s.gkp = [dict(zip(("source_mkp", "akp_element", "akp_type", "tg_id", "tsg_id", "status"), r))
             for r in _rows(conn, sql.SQL("SELECT source_mkp, akp_element, akp_type, tg_id, tsg_id, status "
                                          "FROM {} WHERE is_latest AND instrument = ANY(%s)").format(
                                              _t(schema, tables["gkp"])), (list(instruments),))]
    try:
        s.time_facts = [dict(zip(("target_citation_key", "kind", "effective_from"), (r[0], r[1], str(r[2]) if r[2] else None)))
                        for r in _rows(conn, sql.SQL("SELECT target_citation_key, kind, effective_from FROM {} "
                                                     "WHERE instrument = ANY(%s)").format(_t(schema, tables["time_fact"])),
                                       (list(instruments),))]
    except Exception:                                    # table not created yet (no amendment MKP so far)
        conn.rollback()
    cards = {c: get_field({"RESULTS": r}, "question") for c, r in _rows(
        conn, sql.SQL("SELECT cluster, results FROM {} WHERE type='BATCH' AND is_latest")
        .format(_t(schema, tables["mkp"])))}
    for element, cluster in _rows(conn, sql.SQL("SELECT element, cluster FROM {} WHERE type='MKP' AND is_latest "
                                                "AND split_part(element, '_', 1) = ANY(%s)").format(_t(schema, tables["mkp"])),
                                  (list(instruments),)):
        q = cards.get(cluster)
        if q and q.upper() != "NA":
            s.questions[element] = q
    try:
        for term, results in _rows(conn, sql.SQL("SELECT element, results FROM {} WHERE type='DEF' AND is_latest "
                                                 "AND doc = ANY(%s)").format(_t(schema, tables["ikp"])),
                                   (list(instruments),)):
            s.definitions.append({"term": term, "defined_in": parse_results(results or "").get("defined_in", "")})
    except Exception:
        conn.rollback()
    return s
