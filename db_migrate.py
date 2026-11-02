"""One-time database upgrade to the IKP / MKP / AKP / GKP / governance master names (keeps every row).

Runs automatically the first time a connection is opened in a run (db_insert.get_connection and
ikp_engine.get_connection). It is safe to run again: every step checks first and does nothing when already done.

  tables     pass0_*            -> ikp_*                (pass0_index or ikp_index -> ikp; ikp_versions, ikp_chapters, ikp_nodes, ikp_llm_cache)
             pass_1_mkp*        -> mkp*                 (mkp, mkp_cache, mkp_llm_cache)
             pass_2_akp*        -> akp*                 (akp, akp_cache, akp_llm_cache)
             pass_2_fact / pass_2_atom_fact / pass_2_time_fact -> akp_fact / akp_atom_fact / akp_time_fact
             pass_3_gkp         -> gkp
             pass_3_g18_*       -> gkp_tg18_*
             pass_3_*           -> gkp_*                (gkp_cache, gkp_llm_cache, gkp_coverage, gkp_taxonomy)
             pass_4_law_pack*   -> governance_master*   (governance_master, governance_master_history)
  indexes    renamed with their table (old prefix -> new prefix)
  columns    group_id -> tg_id, subgroup_id -> tsg_id, group_name -> tg_name, subgroup_name -> tsg_name,
             approved_subgroup_id -> approved_tsg_id   (GKP and governance master tables)
  values     G01 -> TG01, G01_S03 -> TSG01_03, G18_P... -> TSG18_P...  (in those id columns)
"""
from __future__ import annotations

MIGRATION_SQL = r"""
DO $mig$
DECLARE
  sch text := %(schema)s;
  r record;
  newname text;
  c record;
BEGIN
  -- views are rebuilt by the governance master; drop them so tables and columns can be renamed
  EXECUTE format('DROP VIEW IF EXISTS %%I.v_molecule_elements CASCADE', sch);
  EXECUTE format('DROP VIEW IF EXISTS %%I.v_element_links CASCADE', sch);

  -- 1. tables (and their indexes)
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = sch AND (
           tablename LIKE 'pass0\_%%' OR tablename = 'ikp_index' OR tablename LIKE 'pass\_1\_mkp%%' OR tablename LIKE 'pass\_2\_%%'
           OR tablename LIKE 'pass\_3\_%%' OR tablename LIKE 'pass\_4\_law\_pack%%') LOOP
    newname := r.tablename;
    newname := regexp_replace(newname, '^pass0_index$', 'ikp');
    newname := regexp_replace(newname, '^ikp_index$', 'ikp');
    newname := regexp_replace(newname, '^pass0_', 'ikp_');
    newname := regexp_replace(newname, '^pass_1_mkp', 'mkp');
    newname := regexp_replace(newname, '^pass_2_akp', 'akp');
    newname := regexp_replace(newname, '^pass_2_(fact|atom_fact|time_fact)$', 'akp_\1');
    newname := regexp_replace(newname, '^pass_3_gkp$', 'gkp');
    newname := regexp_replace(newname, '^pass_3_g18_', 'gkp_tg18_');
    newname := regexp_replace(newname, '^pass_3_', 'gkp_');
    newname := regexp_replace(newname, '^pass_4_law_pack', 'governance_master');
    IF newname <> r.tablename AND to_regclass(format('%%I.%%I', sch, newname)) IS NULL THEN
      EXECUTE format('ALTER TABLE %%I.%%I RENAME TO %%I', sch, r.tablename, newname);
      FOR c IN SELECT indexname FROM pg_indexes WHERE schemaname = sch AND tablename = newname
               AND left(indexname, length(r.tablename)) = r.tablename LOOP
        IF to_regclass(format('%%I.%%I', sch, newname || substr(c.indexname, length(r.tablename) + 1))) IS NULL THEN
          EXECUTE format('ALTER INDEX %%I.%%I RENAME TO %%I', sch, c.indexname,
                         newname || substr(c.indexname, length(r.tablename) + 1));
        END IF;
      END LOOP;
      RAISE NOTICE 'renamed table %% -> %%', r.tablename, newname;
    END IF;
  END LOOP;

  -- 2. columns group / sub-group -> TG / TSG
  FOR c IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = sch AND (table_name LIKE 'gkp%%' OR table_name LIKE 'governance\_master%%')
             AND column_name IN ('group_id', 'subgroup_id', 'group_name', 'subgroup_name', 'approved_subgroup_id') LOOP
    newname := CASE c.column_name WHEN 'group_id' THEN 'tg_id' WHEN 'subgroup_id' THEN 'tsg_id'
                 WHEN 'group_name' THEN 'tg_name' WHEN 'subgroup_name' THEN 'tsg_name' ELSE 'approved_tsg_id' END;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = sch
                   AND table_name = c.table_name AND column_name = newname) THEN
      EXECUTE format('ALTER TABLE %%I.%%I RENAME COLUMN %%I TO %%I', sch, c.table_name, c.column_name, newname);
    END IF;
  END LOOP;

  -- 3. id values G01 -> TG01, G01_S03 -> TSG01_03, G18_P.. -> TSG18_P..
  FOR c IN SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = sch AND (table_name LIKE 'gkp%%' OR table_name LIKE 'governance\_master%%')
             AND column_name IN ('tg_id', 'tsg_id', 'approved_tsg_id', 'proposal_id') AND data_type = 'text' LOOP
    EXECUTE format($q$UPDATE %%I.%%I SET %%I = regexp_replace(regexp_replace(regexp_replace(%%I,
                     '^G(\d\d)_S(\d\d)$', 'TSG\1_\2'), '^G(\d\d)$', 'TG\1'), '^G18_', 'TSG18_')
                   WHERE %%I ~ '^G\d\d'$q$, sch, c.table_name, c.column_name, c.column_name, c.column_name);
  END LOOP;
END
$mig$;
"""

_done = set()


def migrate(conn, schema: str = "public") -> None:
    """Rename old tables / columns / ids once per process. Never deletes data."""
    key = (id(conn), schema)
    if schema in _done or key in _done:
        return
    with conn.cursor() as cur:
        cur.execute(MIGRATION_SQL, {"schema": schema})
    conn.commit()
    _done.add(schema)
