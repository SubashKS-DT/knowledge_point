"""Relationship views for people (the manager's "what is inside one molecule" table) - no new tables.

v_molecule_elements   one row per (molecule, element): every element of the latest AKP split, the facts its atoms
                      read, the sub-groups GKP put it in and the GOVERNANCE MASTER governance masters that use it
                        molecule | mkp_id | instrument | element_type | element_id | concept | detail | version | inserted_at
                      e.g.  CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | CGSTA17 | A   | A_PAY_NONMONEY | Non Money Payment
                            CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | CGSTA17 | F   | F_PAYMENT_FORM | Form of payment
                            CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | CGSTA17 | TSG | TSG01_03        | Barter & Exchange
v_element_links       one row per link inside a molecule (parent -> child), so the tree can be drawn or filtered:
                        molecule | parent_type | parent_id | relation | child_type | child_id
                      relations: HAS (MKP -> LE / OR / CT / TC), USES (LE -> A / G / PD in its expression),
                      MEMBER (G -> A / G / PD), FEEDS (LE -> PD), READS (A -> F), GROUPED_IN (A -> TSG),
                      IN_GOVERNANCE_MASTER (MKP -> governance master)
Both are plain views over the latest rows (is_latest), so they are always current and cost no storage.
"""
from __future__ import annotations

from psycopg2 import sql


def _exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (schema, table))
        return cur.fetchone() is not None


def ensure_views(conn, schema: str, tables: dict, governance_master_table: str) -> list:
    """(Re)create v_molecule_elements and v_element_links. Returns the view names created."""
    T = lambda name: sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))  # noqa: E731
    akp, edge, gkp = tables["akp"], tables["fact"].rsplit("_fact", 1)[0] + "_atom_fact", tables["gkp"]
    have_edge, have_gkp = _exists(conn, schema, edge), _exists(conn, schema, gkp)
    have_rb = _exists(conn, schema, governance_master_table)
    parts = [sql.SQL("""
        SELECT a.source_mkp AS molecule, a.mkp_id, a.instrument, a.type AS element_type, a.element AS element_id,
               NULLIF(a.concept, 'NA') AS concept, left(a.results, 300) AS detail, a.akp_version AS version, a.inserted_at
        FROM {akp} a WHERE a.is_latest AND a.type IN ('MKP','LE','OR','G','PD','A','A_REF','CT','LK','TC')""").format(akp=T(akp))]
    if have_edge:
        parts.append(sql.SQL("""
        SELECT DISTINCT e.source_mkp, a.mkp_id, e.instrument, 'F', e.fact_id, f.name, e.rule, e.akp_version, e.inserted_at
        FROM {edge} e LEFT JOIN {fact} f ON f.fact_id = e.fact_id
        LEFT JOIN {akp} a ON a.source_mkp = e.source_mkp AND a.is_latest AND a.type = 'MKP'
        WHERE e.is_latest""").format(edge=T(edge), fact=T(tables["fact"]), akp=T(akp)))
    if have_gkp:
        parts.append(sql.SQL("""
        SELECT DISTINCT g.source_mkp, g.mkp_id, g.instrument, 'TSG', g.tsg_id, g.tsg_name,
               g.tg_id || ' ' || g.tg_name, g.gkp_version, g.inserted_at
        FROM {gkp} g WHERE g.is_latest AND g.status = 'APPROVED' AND g.tsg_id <> ''""").format(gkp=T(gkp)))
    if have_rb:
        parts.append(sql.SQL("""
        SELECT DISTINCT r.source_mkp, r.mkp_id, NULL, 'GOVERNANCE_MASTER', r.tsg_id, r.tsg_name, r.pack_status, NULL::integer,
               r.updated_at
        FROM {rb} r WHERE r.source_mkp IS NOT NULL AND r.source_mkp <> 'SPINE'""").format(rb=T(governance_master_table)))
    elements = sql.SQL("\nUNION ALL\n").join(parts)

    links = [sql.SQL("""
        SELECT a.source_mkp AS molecule, 'MKP' AS parent_type, a.source_mkp AS parent_id, 'HAS' AS relation,
               a.type AS child_type, a.element AS child_id
        FROM {akp} a WHERE a.is_latest AND a.type IN ('LE','OR','CT','TC')""").format(akp=T(akp)),
        sql.SQL("""
        SELECT a.source_mkp, 'LE', a.element, 'USES', split_part(t.tok[1], '_', 1), t.tok[1]
        FROM {akp} a CROSS JOIN LATERAL regexp_matches(
               COALESCE(substring(a.results from 'expr:\\s*"([^"]*)"'), substring(a.results from 'expr:\\s*([^;]*)'), ''),
               '\\m(?:A|G|PD)_[A-Z0-9_]+', 'g') AS t(tok)
        WHERE a.is_latest AND a.type = 'LE'""").format(akp=T(akp)),
        sql.SQL("""
        SELECT a.source_mkp, 'G', a.element, 'MEMBER', split_part(m.child, '_', 1), m.child
        FROM {akp} a CROSS JOIN LATERAL (
               SELECT btrim(x, ' ''"') AS child
               FROM regexp_split_to_table(COALESCE(substring(a.results from 'members:\\s*\\[([^\\]]*)\\]'), ''), ',') AS x
             ) m
        WHERE a.is_latest AND a.type = 'G' AND m.child <> ''""").format(akp=T(akp)),
        sql.SQL("""
        SELECT a.source_mkp, 'LE', a.element, 'FEEDS', 'PD', m.child
        FROM {akp} a CROSS JOIN LATERAL (
               SELECT btrim(x, ' ''"') AS child
               FROM regexp_split_to_table(COALESCE(substring(a.results from 'feeds:\\s*\\[([^\\]]*)\\]'), ''), ',') AS x
             ) m
        WHERE a.is_latest AND a.type = 'LE' AND m.child LIKE 'PD\\_%'""").format(akp=T(akp))]
    if have_edge:
        links.append(sql.SQL("""
        SELECT e.source_mkp, 'A', e.atom, 'READS', 'F', e.fact_id FROM {edge} e WHERE e.is_latest""").format(edge=T(edge)))
    if have_gkp:
        links.append(sql.SQL("""
        SELECT DISTINCT g.source_mkp, g.akp_type, g.akp_element, 'GROUPED_IN', 'TSG', g.tsg_id
        FROM {gkp} g WHERE g.is_latest AND g.status = 'APPROVED' AND g.tsg_id <> ''""").format(gkp=T(gkp)))
    if have_rb:
        links.append(sql.SQL("""
        SELECT DISTINCT r.source_mkp, 'MKP', r.source_mkp, 'IN_GOVERNANCE_MASTER', 'GOVERNANCE_MASTER', r.tsg_id
        FROM {rb} r WHERE r.source_mkp IS NOT NULL AND r.source_mkp <> 'SPINE'""").format(rb=T(governance_master_table)))
    link_sql = sql.SQL("\nUNION ALL\n").join(links)
    with conn.cursor() as cur:
        for name, body in (("v_molecule_elements", elements), ("v_element_links", link_sql)):
            cur.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(T(name)))
            cur.execute(sql.SQL("CREATE VIEW {} AS ").format(T(name)) + body)
    conn.commit()
    return ["v_molecule_elements", "v_element_links"]
