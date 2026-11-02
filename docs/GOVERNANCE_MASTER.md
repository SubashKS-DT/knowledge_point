# Governance master

A **governance master** holds everything the Decision Graph Engine needs for one taxonomy sub-group (TSG):
- its laws, from every document;
- the laws they depend on;
- the **spine**: "is it a supply?" (with Schedule III) and "goods or services?" (with Schedule II);
- the facts, checks, groups, outcomes, products and steps.

There is **one governance master per sub-group**, kept up to date as laws arrive. Governance master uses no LLM and no engine. The engine simply reads the governance master files from the folder when it needs them.

Code: `governance_master_engine.py` + `governance_master_ss/` (`source`, `build`, `rules`, `pack`, `store`, `excel`, `views`, `tests`).

## 1. When the governance master runs

| When | What it updates |
|---|---|
| **Right after GKP of each document** in `python main.py` (no question asked) | Every TSG the document's atoms were grouped in, plus governance masters that already use those laws (`permission.json` → `governance_master.tsgs` = `"TOUCHED"`). `"ALL"` = every TSG with atoms. `["TSG01_03"]` = only these. |
| `python governance_master_engine.py --tsg TSG01_03` | One governance master (repeat `--tsg` for more) |
| `python governance_master_engine.py --all` | Every TSG that has approved atoms |
| `python governance_master_engine.py --views` | Only (re)create the relationship views |

The screen shows one line per TSG:

```
TSG01_03 : constructing
TSG01_03 : done - 6 steps, 14 checks, 9 facts | laws 7 (2 linked)
```

## 2. How a governance master is updated (append / replace)

| You push | What happens to that sub-group's governance master |
|---|---|
| Section 7, first time | Its laws are **added** |
| Section 7 again, unchanged | Nothing changes: the file is not rewritten and no history row is added |
| Section 7 again, changed (e.g. re-split in AKP) | Section 7's content is **replaced**; the other laws stay |
| Section 25, new | Added to its own sub-groups' governance masters; other governance masters are not touched |

Governance master builds the governance master from **all** its laws in the database, old and new. It replaces the file and the main-table rows only if the result differs from what is there now. Every change is recorded in history as the elements added, changed and removed.

A TSG whose only laws are already in the spine still gets its governance master (the spine itself).

## 3. Where to look

```
Governance_Master/                (outside Output/)
  index.xlsx                      every governance master: sub-group, name, group, status, laws, changes so far, last changed
  TG01/                            group
    TSG01_03/                      sub-group (Barter & Exchange)
      rulebook.yaml               the governance master itself - the engine reads this file (its required name)
      report.xlsx                 Summary, Laws (why each is in), Molecules, Coverage, Facts, Ids, Review
```

**PostgreSQL**

| Table / view | What it holds |
|---|---|
| `governance_master` (**main table**) | The current content of every governance master, one row per element. Filter by `tsg_id`. |
| `governance_master_history` | One row per change: version, date, status, laws, what changed, the full YAML. Insert only. |
| `v_molecule_elements`, `v_element_links` | What is inside each molecule, including the governance masters that use it (see `HOW_IT_WORKS.md` section 7) |

**Columns of the main table `governance_master`:**

| Column | Meaning |
|---|---|
| `tsg_id`, `tsg_name`, `tg_id`, `tg_name` | The sub-group and group |
| `section` | `HEADER` (one header row per governance master), `STEP`, `OUTCOME`, `PRODUCT`, `GROUP`, `CHECK`, `FACT`, `OVERRIDE`, `TRIGGER`, `PRECEDENT` |
| `step_no`, `step_id` | Which step of the decision the element belongs to, in order (step 1 = "Is the transaction a supply?") |
| `element_id`, `element_type` | The element's id in the governance master, and its kind (`GATE` = a step that stops the decision on "no") |
| `text` | The plain words: statement, question, name or description |
| `logic` | The rule, formula, group members or trigger |
| `check_order` | For checks: `LOW` (decided by a rule) or `HIGH` (judge). For facts: the source tier `T0` (data) or `T1` (light model). |
| `facts` | The facts a check or review flag reads |
| `source_mkp`, `mkp_id`, `akp_id`, `citation` | Which law the element came from (`SPINE` for the spine) |
| `included_as` | `SPINE`, `MEMBER`, or why a linked law came in: `PRODUCT PD_…`, `REFERENCE …`, `OVERRIDE …`, `DEFINITION …` |
| `attributes` | The element exactly as in `rulebook.yaml` (JSON) |
| `pack_status` | `APPROVED`, or `NEEDS_REVIEW` when the self-check found a problem |
| `updated_at` | When this content was written |

**Example queries:**

```sql
-- everything in the Barter governance master, step by step
SELECT step_no, section, element_id, text, logic, included_as
FROM governance_master WHERE tsg_id = 'TSG01_03' ORDER BY step_no NULLS LAST, section, element_id;

-- which governance masters use section 7
SELECT DISTINCT tsg_id, tsg_name FROM governance_master WHERE citation LIKE 'CGSTA17_S7%';

-- what changed each time
SELECT version, changed_at, pack_status, change FROM governance_master_history WHERE tsg_id = 'TSG01_03' ORDER BY version;
```

## 4. What is checked (in Python, no engine)

These are checked before the governance master is written:
- Every rule uses the engine's grammar and only known facts.
- Every judged check has its question and known facts.
- Every group member, formula id, product, override, review flag and precedent refers to something that exists.
- No outcome sits in two steps.
- Every product a step uses is produced by an earlier step.

Anything wrong is listed in `report.xlsx` → **Review**, and the governance master is marked `NEEDS_REVIEW`. It is still written, so it can be looked at.

## 5. How the content is built

1. **Read** the latest MKP, AKP and GKP rows: one snapshot per run.
2. **Members:** MKPs with an approved GKP atom in the sub-group.
3. **Links,** followed up to `prerequisite_depth` steps:
   - products a law needs (e.g. Rule 27 needs s.15(4));
   - references in its atoms;
   - overrides (e.g. a proviso);
   - definitions of terms its atoms use.
4. **Spine:** MKPs already in the spine (`Config/governance_master_map.json` → `spine.covers`) are not converted again. The spine file is `Config/spine/gst_s7_spine.yaml`.
5. **Convert** AKP rows into governance master elements (the full mapping is in the table below):
   - ids that clash get the molecule's tag;
   - rules are cleaned into the engine grammar;
   - a rule the grammar cannot express becomes a judge question, with a warning.
6. **Order the steps:** spine first, then definitions, then each law after the laws whose products it needs.
7. **Law as on a date** (`governance_master.law_as_on`): provisions not in force on that date are left out, using the amendment dates.

| AKP | Governance master | Note |
|---|---|---|
| FACTS / fact registry | `facts` | Types mapped (BOOL→BOOLEAN, NUMBER / PERCENT / QUANTITY / PERIOD→AMOUNT, ID / LIST→TEXT). Tier from resolver or evidence. Shared spine facts via `fact_aliases`. |
| A (LOW) | `predicates` with `rule` | cleaned: `"x"`→`'x'`, `==`→`=`, `= 'TRUE'`→`= TRUE`, `IS NOT NULL`→`IS_SET` |
| A (HIGH) | `predicates` with `interpretation` + `grounding` | |
| G | `groups` | ALL / ANY. A one-member group is replaced by its member. |
| LE | `outcomes` + one `product` each | |
| OR exclusive | step `exclusive` | |
| LK overrides | `overrides` on the overridden law's step | |
| CT | `triggers` | review flags; they never change a verdict |
| MKP + cluster question | `compounds` (steps) | one step per law |

## 6. Settings

`permission.json` → `governance_master` (day to day):

```json
"governance_master": {"tsgs": "TOUCHED", "law_as_on": ""}
```

`Config/settings.json` → `governance_master` (fixed): `save_to_db`, `prerequisite_depth` (1 = only the laws a member
law points to directly), `merge_clauses` (clauses of one provision, e.g. s.2(31)(a) and (b), become one step),
table names, `map_file`, `spine_rulebook`, `output_dir` (`Governance_Master`).

`permission.json` → `use_cache.governance_master: false` rewrites every checked governance master and adds a history
row even when nothing changed.

## 7. Tests

`python -m pytest -q governance_master_ss/tests` runs without a database. It covers:
- members, links and the spine;
- ids, rules and facts;
- the Python self-check;
- the main-table rows;
- the change summary;
- the fingerprint;
- the TSGs of the MKPs a run touched;
- clause steps, schedule references, value aliases and the LOW / HIGH order check (`test_gm_fixes.py`);
- a file written once and left alone when unchanged;
- spine-only sub-groups.
