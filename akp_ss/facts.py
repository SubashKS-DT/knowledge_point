"""AKP / the fact registry - the backbone of the fact network.

A FACT is an observable input about a real case, with its evidence (e.g. F_PERSON_DATE_OF_BIRTH, evidence Aadhaar;
F_SUPPLY_CONSIDERATION = PAID / NONE, evidence invoice). Atoms TEST facts. Atoms of different laws that test
the same fact are connected through it - so the same fact must keep ONE id everywhere.

akp_fact        one row per canonical fact: id, name, entity, type, unit, options, evidence, derived_from, embedding
akp_atom_fact   edge atom -> fact (with the atom's rule), per AKP version, is_latest
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Tuple

from psycopg2 import sql

from transport import get_field

from .parse import field_list, rfield

DATA_TYPES = {"BOOL", "BOOLEAN", "CODE", "ENUM", "AMOUNT", "MONEY", "NUMBER", "INTEGER", "PERCENT", "RATE", "DATE",
              "PERIOD", "DURATION", "TEXT", "STRING", "LIST", "ID", "QUANTITY"}
CANON_TYPE = {"BOOLEAN": "BOOL", "ENUM": "CODE", "MONEY": "AMOUNT", "INTEGER": "NUMBER", "RATE": "PERCENT",
              "STRING": "TEXT", "DURATION": "PERIOD"}


def canon_type(t: str) -> str:
    t = (t or "").strip().upper()
    return CANON_TYPE.get(t, t)


def _vec(v) -> Optional[str]:
    return "[" + ",".join(str(float(x)) for x in v) + "]" if v else None


class FactRegistry:
    def __init__(self, conn, schema: str, prefix: str = "pass_2"):
        self.conn, self.schema = conn, schema
        self.fact_t, self.edge_t = f"{prefix}_fact", f"{prefix}_atom_fact"

    def t(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def ensure(self):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                fact_id text PRIMARY KEY, name text NOT NULL, entity text, data_type text, unit text,
                options jsonb NOT NULL DEFAULT '[]', evidence jsonb NOT NULL DEFAULT '[]', derived_from jsonb NOT NULL DEFAULT '[]',
                nl text, first_instrument text, first_source_mkp text, possible_duplicate_of text,
                embedding vector, inserted_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""")
                        .format(self.t(self.fact_t)))
            cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                instrument text NOT NULL, version_id text NOT NULL, source_mkp text NOT NULL, akp_version integer NOT NULL,
                atom text NOT NULL, fact_id text NOT NULL, rule text, is_latest boolean NOT NULL DEFAULT true,
                inserted_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (instrument, version_id, source_mkp, akp_version, atom, fact_id))""").format(self.t(self.edge_t)))
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (fact_id) WHERE is_latest").format(
                sql.Identifier(f"{self.edge_t}_fact_idx"), self.t(self.edge_t)))
        self.conn.commit()

    # ---------------- reads
    def all_ids(self) -> Dict[str, dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT fact_id, name, data_type FROM {}").format(self.t(self.fact_t)))
            return {f: {"name": n, "type": t} for f, n, t in cur.fetchall()}

    def slice(self, unit_prefix: str, vector=None, limit: int = 30) -> List[dict]:
        """Facts the models should reuse: those already used in the same unit, then the nearest by meaning,
        then the most used."""
        cols = "f.fact_id, f.name, f.entity, f.data_type, f.unit, f.options, f.evidence"
        out, seen = [], set()

        def add(rows):
            for r in rows:
                if r[0] not in seen and len(out) < limit:
                    seen.add(r[0])
                    out.append({"fact_id": r[0], "name": r[1], "entity": r[2], "type": r[3], "unit": r[4],
                                "options": r[5], "evidence": r[6]})
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL(f"SELECT DISTINCT {cols} FROM {{}} f JOIN {{}} e ON e.fact_id=f.fact_id "
                                "WHERE e.is_latest AND e.source_mkp LIKE %s").format(self.t(self.fact_t), self.t(self.edge_t)),
                        (unit_prefix + "%",))
            add(cur.fetchall())
            if vector:
                try:
                    cur.execute(sql.SQL(f"SELECT {cols} FROM {{}} f WHERE embedding IS NOT NULL AND vector_dims(embedding)=%s "
                                        "ORDER BY embedding <=> %s::vector LIMIT %s").format(self.t(self.fact_t)),
                                (len(vector), _vec(vector), limit))
                    add(cur.fetchall())
                except Exception:
                    self.conn.rollback()
            cur.execute(sql.SQL(f"SELECT {cols} FROM {{}} f LEFT JOIN {{}} e ON e.fact_id=f.fact_id AND e.is_latest "
                                "GROUP BY f.fact_id ORDER BY count(e.atom) DESC, f.fact_id LIMIT %s")
                        .format(self.t(self.fact_t), self.t(self.edge_t)), (limit,))
            add(cur.fetchall())
        return out

    def export_rows(self):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT f.fact_id, f.name, f.entity, f.data_type, f.unit, f.options::text, f.evidence::text,
                count(e.atom), f.possible_duplicate_of, f.first_source_mkp FROM {} f LEFT JOIN {} e
                ON e.fact_id=f.fact_id AND e.is_latest GROUP BY f.fact_id ORDER BY f.fact_id""")
                        .format(self.t(self.fact_t), self.t(self.edge_t)))
            return cur.fetchall()

    # ---------------- check the Standard table against the registry
    def check(self, rows: List[dict]) -> List[str]:
        known = self.all_ids()
        errors = []
        for r in rows:
            if r["TYPE"] != "FACTS":
                continue
            fid, t = r["ELEMENT"], canon_type(rfield(r, "type"))
            if t and t not in {canon_type(x) for x in DATA_TYPES}:
                errors.append(f"{fid}: type '{rfield(r, 'type')}' - use one of BOOL, CODE, AMOUNT, NUMBER, PERCENT, DATE, PERIOD, TEXT, LIST, ID, QUANTITY")
            if fid in known and known[fid]["type"] and t and known[fid]["type"] != t:
                errors.append(f"{fid}: already registered as {known[fid]['type']} ('{known[fid]['name']}') - "
                              f"reuse it with that type or use a different fact id")
        return errors

    # ---------------- save (inside the caller's transaction)
    def save(self, cur, rows: List[dict], instrument: str, version: str, source_mkp: str, akp_version: int,
             embed: Optional[Callable] = None) -> Tuple[int, int]:
        known = self.all_ids()
        new = 0
        for r in rows:
            if r["TYPE"] != "FACTS":
                continue
            fid = r["ELEMENT"]
            options = field_list(r, "options")
            evidence = field_list(r, "evidence") or ([get_field(r, "evidence")] if get_field(r, "evidence") else [])
            derived = field_list(r, "derived_from")
            if fid in known:
                cur.execute(sql.SQL("""UPDATE {} SET
                    options = (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]') FROM jsonb_array_elements(options || %s::jsonb) x),
                    evidence = (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]') FROM jsonb_array_elements(evidence || %s::jsonb) x),
                    updated_at = now() WHERE fact_id=%s""").format(self.t(self.fact_t)),
                            (json.dumps(options), json.dumps(evidence), fid))
                continue
            name = get_field(r, "name") or fid
            nl = get_field(r, "nl")
            vector, dup = None, None
            if embed:
                try:
                    vector = embed(f"{name} - {nl}".strip(" -"))
                except Exception:
                    vector = None
            if vector:
                cur.execute(sql.SQL("SELECT fact_id, 1 - (embedding <=> %s::vector) FROM {} WHERE embedding IS NOT NULL "
                                    "AND vector_dims(embedding)=%s ORDER BY embedding <=> %s::vector LIMIT 1")
                            .format(self.t(self.fact_t)), (_vec(vector), len(vector), _vec(vector)))
                hit = cur.fetchone()
                if hit and hit[1] is not None and float(hit[1]) >= 0.93:
                    dup = hit[0]
            entity = get_field(r, "entity") or (fid.split("_")[1] if fid.count("_") >= 2 else "")
            cur.execute(sql.SQL("""INSERT INTO {} (fact_id, name, entity, data_type, unit, options, evidence, derived_from, nl,
                first_instrument, first_source_mkp, possible_duplicate_of, embedding)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::vector) ON CONFLICT (fact_id) DO NOTHING""")
                        .format(self.t(self.fact_t)),
                        (fid, name, entity, canon_type(rfield(r, "type")), get_field(r, "unit") or None, json.dumps(options),
                         json.dumps(evidence), json.dumps(derived), nl or None, instrument, source_mkp, dup, _vec(vector)))
            new += 1
            known[fid] = {"name": name, "type": canon_type(rfield(r, "type"))}
        # edges atom -> fact; earlier AKP versions of this MKP are no longer the latest
        cur.execute(sql.SQL("UPDATE {} SET is_latest=false WHERE instrument=%s AND version_id=%s AND source_mkp=%s AND is_latest")
                    .format(self.t(self.edge_t)), (instrument, version, source_mkp))
        edges = 0
        for r in rows:
            if r["TYPE"] not in ("A", "A_REF"):        # an A_REF (SAME) atom still tests its facts
                continue
            for fid in dict.fromkeys(r.get("_facts") or field_list(r, "facts")):
                cur.execute(sql.SQL("""INSERT INTO {} (instrument, version_id, source_mkp, akp_version, atom, fact_id, rule)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""").format(self.t(self.edge_t)),
                            (instrument, version, source_mkp, akp_version, r["ELEMENT"], fid, get_field(r, "rule") or None))
                edges += 1
        return new, edges


# ---------------------------------------------------------------- non-operative MKPs (no LLM)

AMEND_KIND = [(r"\bsub\w*?tuted\b|\bsubs\.", "SUBSTITUTED"), (r"\binserted\b|\bins\.", "INSERTED"),
              (r"\bomitted\b|\bomit\b", "OMITTED"), (r"\badded\b", "INSERTED"), (r"\bre-?numbered\b", "RENUMBERED"),
              (r"^\s*ibid\b", "IBID")]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
DATE = (r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})"
        r"|(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?(january|february|march|april|may|june|july|august|september|october|november|december),?\s*(\d{4})")


def _iso(text: str) -> Optional[str]:
    m = re.search(DATE, text or "", re.I)
    if not m:
        return None
    if m.group(1):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo, y = int(m.group(4)), MONTHS[m.group(5).lower()], int(m.group(6))
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else None


def time_fact(mkp_row: dict, instrument: str, source_mkp: str) -> dict:
    """An AMENDMENT MKP becomes a time fact on the provision it amends (not an atom).
    Footnote styles handled: 'Inserted vide Notf no. 35/2021-CT dt. 24.09.2021 w.e.f. 01.01.2022',
    'Inserted by The ... (Amendment) Act, 2018 (No. 31 of 2018) - Brought into force w.e.f. 01st February, 2019',
    'Substituted vide Notf No. 16/2019-CT dt.29.03.2019 wef 01.04.2019 for ―old wording‖', 'Omitted ...', 'Ibid.'"""
    text = get_field(mkp_row, "text")
    note = (text.split("states:", 1)[1] if "states:" in text else text).strip()
    kind = next((k for pat, k in AMEND_KIND if re.search(pat, note, re.I)), "AMENDED")
    by = (re.search(r"(?:\bvide|\bby)\s*((?:Notf|Notification|Order|Circular)\.?\s*no\.?\s*[\w/.-]+(?:\s*[–-]\s*CT)?)", note, re.I)
          or re.search(r"(?:\bvide|\bby)\s+((?:The\s+)?[A-Z][^–;]*?\bAct\b,?\s*\d{4}(?:\s*\(No\.\s*\d+\s+of\s+\d{4}\))?)", note))
    dated = re.search(r"\b(?:dt|dated)\.?\s*(" + DATE + ")", note, re.I)
    wef = re.search(r"(?:w\.?\s*e\.?\s*f\.?|with effect from)\s*(?:the\s+)?(" + DATE + ")", note, re.I)
    valid = get_field(mkp_row, "valid_from")
    effective = valid if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid or "") else (_iso(wef.group(1)) if wef else None)
    replaced = re.search(r"\bfor\s+[―\"“]\s*(.+?)\s*[‖\"”]", note, re.S)
    citation_key = get_field(mkp_row, "citation_key") or source_mkp.split("#", 1)[0]
    return {"time_fact_id": "T_" + source_mkp.replace("#", "_"), "target": citation_key, "kind": kind,
            "amended_by": re.sub(r"\s+", " ", by.group(1)).strip(" .") if by else None,
            "instrument_date": _iso(dated.group(1)) if dated else None,
            "effective_from": effective, "replaced_text": replaced.group(1).strip() if replaced else None, "note": note}


def skip_rows(bundle: dict, knowledge_type: str, tfact: Optional[dict] = None) -> List[dict]:
    """MKP + SP (+ LK for amendments) + SUM rows for HEADING / AMENDMENT / METADATA MKPs - written by Python."""
    mkp_id, row = bundle["mkp_id"], bundle["mkp_row"]

    def q(t):
        return '"' + str(t or "").replace(";", ",").replace("|", "/").replace('"', '\\"') + '"'
    reason = {"HEADING": "Heading - names the subject, no testable condition",
              "AMENDMENT": "Amendment record - kept as a time fact on the amended provision",
              "METADATA": "Instrument metadata - no testable condition"}.get(knowledge_type, "Non-operative")
    rows = [{"MKP": mkp_id, "TYPE": "MKP", "ELEMENT": mkp_id, "CONCEPT": "NA",
             "RESULTS": (f"citation: {q(get_field(row, 'citation'))}; citation_key: {get_field(row, 'citation_key')}; "
                         f"part: {get_field(row, 'part')}; knowledge_type: {knowledge_type}; frame: {knowledge_type}; "
                         f"skip: {q(reason)}; nl: {q(get_field(row, 'text'))}")}]
    if tfact:
        rows.append({"MKP": mkp_id, "TYPE": "LK", "ELEMENT": "LK_" + tfact["time_fact_id"], "CONCEPT": "NA",
                     "RESULTS": (f"relation: AMENDS; target: {tfact['target']}; status: TIME_FACT; time_fact: {tfact['time_fact_id']}; "
                                 f"kind: {tfact['kind']}; effective_from: {tfact['effective_from'] or 'NA'}; "
                                 f"instrument_date: {tfact['instrument_date'] or 'NA'}; "
                                 f"amended_by: {q(tfact['amended_by'] or 'NA')}")})
    for sp in bundle["sp_rows"]:
        rows.append({"MKP": mkp_id, "TYPE": "SP", "ELEMENT": get_field(sp, "sp") or "SP1", "CONCEPT": "NA",
                     "RESULTS": f"role: FRAME; text: {q(get_field(sp, 'text'))}; elements: [{mkp_id}]"})
    rows.append({"MKP": mkp_id, "TYPE": "SUM", "ELEMENT": mkp_id, "CONCEPT": "NA",
                 "RESULTS": f"atoms: 0; facts: 0; skip: {q(reason)}"})
    return rows
