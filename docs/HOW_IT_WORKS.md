# How LAW ARC works (IKP → MKP → AKP → GKP → governance master)

## 1. The run: `python main.py`

**Two settings files:**
- `permission.json` — what you change day to day: which steps run (`run`), cache on/off (`use_cache`), which kinds
  of law to read (`read`: acts / rules / notifications / circulars), the scope question, which TSGs get a governance
  master, and which LLM + model does each job (`llm`, e.g. `{"llm": "CLAUDE", "model": "claude-sonnet-5-5"}`; Gemini
  and Claude can be swapped per job).
- `Config/settings.json` — fixed settings: folders, prompt files, table names, limits, word lists.

**Law folder.** Put PDFs in `Law/Acts`, `Law/Rules`, `Law/Notifications`, `Law/Circulars`. The name, code and type of
each PDF are taken from its file name and kept in `Config/documents.json` (edit there if you want a different code).
A PDF left directly in `Law/` is moved to its folder when its kind is clear from the name.

**Order of work:**
1. **Check.** `config_check.check_permission()` stops the run before any step if a prompt, folder, taxonomy, spine or
   PDF is missing, listing every problem. The first database connection also upgrades old table names (`db_migrate.py`).
2. **IKP** for every selected PDF.
3. **MKP** asks the scope once per document, then goes **one legal unit at a time**. After each unit:
   **AKP** → **GKP** on that unit, with no question asked.
4. When the document is done, the **governance master** of every TSG its atoms were grouped in is built
   (`TSG01_03 : constructing` … `done`), then the **summary** is shown, then the next document starts.
5. If MKP is switched off, AKP / GKP / governance master run on the MKPs already saved.

### Rules every step follows

- **Database first.** MKP, AKP and GKP look in the database before calling an LLM:
  - MKP: the saved MKP text is compared with the law text Python reads from the PDF. Same → reused. Missing or
    different → LLM. The screen says why, e.g. `MKP LLM reason : CGSTA17_S7_SS1_Ca#TEXT - text changed` with the
    old (v1) and new (v2) text.
  - AKP: reused when it was made from the same MKP version. GKP: reused when it was made from the same AKP version.
- **New version only when different.** A re-run that gives the same content keeps the version number. A different
  result is saved as the next version; older rows stay with `is_latest=false`. Nothing is deleted.
- **Python checks every model answer** before it is saved (word checks, structure, facts, engine grammar).
- **Short screen, full log.** The screen shows only the main lines and the summary; everything else is in
  `Output/logs/law_arc_<date>.log`.

---

## 2. IKP: the book's structure (`ikp_engine.py`, `ikp_ss/`)

**Input:** one PDF from `Law/`. **Output:** a verified tree of every legal unit.

| Step | Module | What happens |
|---|---|---|
| Read | `layout.py` | Text is read with PyMuPDF. Running headers and footers, contents pages, footnotes, margin notes and footnote markers are separated. `[[PAGE n]]` markers are kept. |
| Split | `structure.py` | Python splits the document: chapter → section / rule → sub-section → clause → sub-clause → proviso / explanation / closing text / table rows / annexures / orders. |
| Enrich | `enrich.py`, `citations.py` | Status (inserted / substituted / omitted) and amendment dates are taken from the footnotes. Cross-references such as "section 16(2)(c)" and "sections 73 to 76" are resolved, and definitions are collected. |
| Cluster | `cluster.py` | One cluster (C_ / CM_ rows) per top-level operative provision. |
| Gate | `gates.py` | A quality check per chapter: coverage, sequence, duplicate keys, empty provisions and unresolved markers. |
| LLM help | `llm_assist.py` | **Only** for a chapter that fails the gate (`llm.mode=ON_FAIL`). The model tags line starts, and Python re-checks them. The LLM version is kept only if it scores better. |
| Save | `build.py`, `store.py` | Each chapter is committed as a checkpoint (resume point). When the document gate passes, the version becomes COMPLETE and `is_latest`. |
| Read back | `reader.py` | `IkpIndex`: `get`, `resolve("section 7(1)(a)")`, `context`, `read_from_pdf`. This is the only way MKP gets law text. |

**Switches** (`permission.json` → `run.ikp` / `use_cache.ikp`; `Config/settings.json` → `ikp`):

| Setting | Effect |
|---|---|
| `run.ikp=false` | Use the latest saved IKP. |
| `use_cache.ikp=true` | Reuse the same PDF if already COMPLETE, or resume an unfinished run. |
| `use_cache.ikp=false` | Build a new version. |
| `llm_mode` | `OFF`, `ON_FAIL` or `ALWAYS`. |
| `llm_chapters` | Re-check only the listed chapters with the LLM. |
| `fail_on_gate` | A document that fails is saved but not made latest. |
| `dry_run_fetch` | Every operative node must return non-empty text. |

**Tables:**

| Table | Holds |
|---|---|
| `ikp_versions` | One row per document version: `version_no`, PDF SHA-256, law-as-on date, status, `is_latest` |
| `ikp_chapters` | Per-chapter checkpoint |
| `ikp` | DOC / TYPE / ELEMENT / PAGE_NUMBER / RESULTS rows |
| `ikp_nodes` | One row per node: text, PDF blocks, anchors, parent, children, amendments, cross-references |
| `ikp_llm_cache` | Raw LLM answers |

**Also written:** `Output/ikp/<CODE>.ikp.jsonl` and `Output/ikp.xlsx`.

---

## 3. MKP: molecules (`mkp_engine.py`, `mkp_ss/`)

**Input:** the latest IKP. The PDF hash must match the IKP, otherwise MKP stops and asks you to re-run IKP. **Output:** Standard MKP + SP rows in `mkp`.

1. **Scope** (`scope.py`):
   - If `ask_user_input=true`, a menu: 1 Full document, 2 Chapter → Section/Rule → Sub-section → Clause (0 = the whole of this level), 3 Type a citation, 4 Type a IKP key.
   - Otherwise `permission.json` → `scope.default` is used, e.g. `"FULL"` or `["section 7", "rule 55(1)"]`.
   - The scope is split into batches: one per legal unit, or one for the smaller selected piece.
2. **Reference text** (`draft.py`): Python writes the exact reference text of every expected MKP from the IKP, including lead-ins, closing lines and footnotes.
3. **Models by complexity** (`scope.complexity`): `SIMPLE` or `COMPLEX` tier, which picks the LLM + model of each job from `permission.json` → `llm.mkp`.
4. **Generate:** generator_1 and generator_2 run in parallel with `law_arc_mkp_prompt.txt`.
5. **Pre-gate report** (`check.pregate`): Python compares both answers with the reference text and tells the judge exactly where each one differs.
6. **Judge:** the judge model, using the standard judge prompt `law_arc_mkp_judge_prompt.txt`, builds the FINAL table.
7. **Standard gate** (`check.standard_gate`):
   - Every expected provision has an MKP.
   - **Source fidelity:** every word comes from the source (`fidelity_check=BLOCK`).
   - The SP phrases rejoin exactly to the MKP text.
   - Modal, negation and qualifier words (`Config/settings.json` → `word_lists`) are all kept. This is the anti-hallucination word check: a model may not drop "shall", "not" or "subject to".

   On failure, the judge gets one retry with the exact errors. If it still fails, the unit is **split into its children**, each child is run on its own, and the failure is logged.
8. **Save** (`store.commit`, one transaction per unit) with the MKP embedding. **Versioning:**
   - each MKP element gets `mkp_version` 1, 2, 3, …;
   - only an element whose text, type or phrases changed gets the next version; the earlier row gets `is_latest=false`;
   - nothing is deleted;
   - all readers use `is_latest`.

**Tables:**

| Table | Holds |
|---|---|
| `mkp` | `cluster`, `type`, `element`, `results`, `embedding`, `version_id` (PDF hash), `mkp_id`, `mkp_version`, `is_latest`, `run_id`, `inserted_at`, `source_version` (IKP version), `qc_status`, `doc_type`, `instrument_code` |
| `mkp_cache` | generator_1 / generator_2 / Standard rows per unit and input hash |
| `mkp_llm_cache` | Raw model answers |

**Also written:**
- `Output/mkp_act.xlsx`
- `Output/mkp_completed_provisions.txt`
- `Output/mkp_failed_provisions.txt` and `Output/mkp_failed_details.txt`

---

## 4. AKP: atoms and facts (`akp_engine.py`, `akp_ss/`)

**Input:** the latest approved Standard MKPs of the scope, with children before parents. **Output:** the element table per MKP, plus the fact network.

1. **Python-written MKPs (no LLM):**
   - HEADING and METADATA MKPs get MKP + SP + SUM rows.
   - AMENDMENT MKPs also become a **time fact** (`akp_ss/facts.time_fact`): kind (INSERTED / SUBSTITUTED / OMITTED), the instrument that made the change, the instrument date, the effective-from date and the replaced text. It is saved in `akp_time_fact` with an LK `AMENDS` link to the provision it changes.
2. **Operative MKPs:**
   1. generator_1 and generator_2 run with `law_arc_akp_prompt.txt`. The input carries the MKP, its SP phrases, the cluster, the taxonomy (as guidance only) and a **fact registry slice**: the facts already used in this section, then the nearest by meaning, then the most used.
   2. The judge (`law_arc_akp_judge_prompt.txt`), returns one Standard table: `MKP|TYPE|ELEMENT|CONCEPT|RESULTS`, with rows MKP, LE, OR, G, PD, A, CT, FACTS, LK, SP, LW, TC and SUM.
   3. **Python checks** (`guard.py`):
      - `legal_words`: every negation stays next to the word it negates, so "without consideration" turned into "for consideration" is caught as **NEGATION FLIPPED**. It also checks that qualifiers (notwithstanding, subject to, only, within …) are kept, that shall / may are not changed, and that no number is lost or invented.
      - `structure`: IDs, LE and G logic (LEs use A_ / G_ / PD_ only, never F_), groups of two or more members, no orphan atoms, full-sentence statements, **every operative atom names its fact(s)**, every fact declared, and SP coverage.
      - `registry.check`: a fact type must match the type already registered.
      - `refs`: an atom may not cite a provision the source does not cite.
      - `engine_ready`: `order` is LOW or HIGH; a LOW rule uses the engine grammar, only declared facts and only their options; a HIGH atom has its question and facts.

      Errors go back to the judge (`judge_repairs`). If errors remain, the attempt counts as FALSE.
   4. **Comparison** (`law_arc_akp_comparison_prompt.txt`), run by the `comparison` model: rebuild the MKP from the atoms and check it means the same as the source. If FALSE, the reasons are fed into the next attempt (`max_attempts`).
   5. **When TRUE:**
      - Python adds the reference links.
      - The atoms are embedded (concept + family, and statement).
      - The **SAME check** (`law_arc_akp_same_prompt.txt`) asks which new atoms already exist, comparing only against existing atoms with similarity above 0.90. An atom confirmed SAME becomes `A_REF` pointing to the existing `akp_id`. Its fact links stay; its text is not stored twice.
3. **Save** (one transaction):
   - `akp`: next `akp_version`; older rows get `is_latest=false` and are marked SUPERSEDED.
   - `akp_fact`: new facts. For a known fact, new options and evidence are merged in.
   - `akp_atom_fact`: atom → fact edges, carrying the atom's rule.

**The fact network starts here.**
- One fact keeps **one id everywhere**, e.g. `F_SUPPLY_CONSIDERATION` (PAID / AGREED / NONE).
- Atoms of different provisions that test the same fact are linked through it.
- `derived_from` records computed facts, such as age from date of birth.
- `evidence` lists the documents that prove a fact.
- `possible_duplicate_of` flags a new fact whose meaning is ≥ 0.93 similar to an existing one.

**Tables:**

| Table | Holds |
|---|---|
| `akp` | `instrument`, `version_id`, `source_mkp`, `mkp_id`, `ordinal`, `type`, `element`, `concept`, `results`, `concept_embedding`, `atom_embedding`, `validation_status`, `akp_version`, `is_latest`, `inserted_at`, `mkp_version`, `tier`, `akp_id`, `same_akp_id` |
| `akp_cache` | Cached results per MKP and input hash |
| `akp_llm_cache` | Raw model answers |
| `akp_fact` | One row per canonical fact |
| `akp_atom_fact` | Atom → fact edges |
| `akp_time_fact` | Amendments as time facts |

**Also written:** `Output/akp.xlsx` and `Output/akp_failed_mkps.txt`.

---

## 5. GKP: grouping into TSGs (`gkp_engine.py`, `gkp_ss/`)

**Input:** the latest approved AKP rows of the scope, plus `Config/gst_taxonomy.json` (18 TGs, 129 TSGs, each with definition, include, exclude, key provisions and anchors). **Output:** one row per (atom, sub-group) membership.

1. **Taxonomy sync** (`taxonomy.py`): TSG embeddings are stored in `gkp_taxonomy` and re-embedded when the definitions change.
2. **Candidates per atom** (`candidates.py`):
   - concept-embedding and atom-embedding nearest TSGs;
   - include-term hits;
   - **anchor provisions**: the most specific anchor wins;
   - TSGs of atoms that share a fact;
   - TSGs of referenced provisions;
   - section neighbours.
3. **Decide** (`verify.py`, `law_arc_gkp_prompt.txt`):
   1. verifier_1 and verifier_2 check each (atom, candidate) pair in parallel. If they agree, the pair is APPROVED. If they disagree, the judge decides.
   2. Atoms with nothing approved get a wider embedding search inside TG01–TG17.
   3. Then the TG18 fallback.
   4. Then a **TG18 proposal**, a suggested new TSG that waits for you: `python gkp_engine.py --approve-tg18 <id>` or `--reject-tg18 <id>`.
4. **Special cases:**
   - `A_REF` atoms inherit the groups of the atom they are SAME as.
   - AMENDMENT MKPs go to TSG16_06 plus the groups of the provision they amend.
   - HEADING MKPs are skipped.
5. **Save:** the next `gkp_version` per MKP, with `is_latest`, `inserted_at`, TG and TSG ids and names, `status` (APPROVED / REVIEW / UNCLASSIFIED), `method`, `reason` and scores.
6. **Coverage check:** any anchor provision whose atoms did not land in the TSG that names it is reported as a GAP.

**Tables:**

| Table | Holds |
|---|---|
| `gkp` | The memberships |
| `gkp_taxonomy` | TSG embeddings |
| `gkp_tg18_proposals` | Proposed new TG18 TSGs |
| `gkp_coverage` | The coverage check |
| `gkp_cache` | Cached results per MKP and input hash |
| `gkp_llm_cache` | Raw model answers |

**Also written:** `Output/gkp.xlsx`.

**Screen:** at the end of GKP, the number of atoms per TSG.

---

## 6. Governance master: one per TSG (`governance_master_engine.py`, `governance_master_ss/`)

A governance master holds everything the Decision Graph Engine needs for one TSG: its laws, the laws they point to
(schedules, rules, definitions), and the spine ("is it a supply?", "goods or services?").

- It runs right after GKP of each document, for every TSG the atoms were grouped in. Every TSG with laws gets one,
  even when its laws are all in the spine.
- It is checked in Python (ids, rules, steps, LOW/HIGH order). No LLM, no engine.
- Same content → the file is not rewritten. Different → the file and the main-table rows are replaced and the change
  is recorded in history.

| Output | What it is |
|---|---|
| `Governance_Master/<TG>/<TSG>/rulebook.yaml` | The governance master; the engine reads this file |
| `report.xlsx` | Next to each governance master |
| `Governance_Master/index.xlsx` | Every governance master |
| `governance_master` | **Main table**: one row per element, filter by `tsg_id` |
| `governance_master_history` | One row per change |

Full details are in `docs/GOVERNANCE_MASTER.md`.

## 7. What one molecule contains (manager view)

There are two PostgreSQL **views**: always current, no extra tables, no storage. The governance master step refreshes them on every run. To refresh them at any time:

```
python governance_master_engine.py --views
```

**`v_molecule_elements`**: everything inside a molecule, one row per element (the manager's `M1 | A | A1` table).

| molecule | mkp_id | element_type | element_id | concept |
|---|---|---|---|---|
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | LE | LE_CIPFS | |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | G | G_PAY_FORM | |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | A | A_PAY_NONMONEY | Non Money Payment |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | F | F_PAYMENT_FORM | Form of payment |
| CGSTA17_S2_C31_SCa#TEXT | M_CIPFS | TSG | TSG01_03 | Barter & Exchange |
| CGSTA17_S2_C31_SCa#TEXT | | GOVERNANCE_MASTER | TSG01_03 | Barter & Exchange |

The element types are:
- MKP, LE, OR, G, PD, A, A_REF, CT, LK and TC, from AKP;
- F: the facts its atoms read;
- TSG: the TSGs from GKP;
- GOVERNANCE_MASTER: the governance masters that use it.

There are also `detail`, `version` and `inserted_at` columns.

**`v_element_links`**: how the elements connect inside the molecule, one row per link (parent → child):

| relation | parent → child | example |
|---|---|---|
| HAS | MKP → LE / OR / CT / TC | the molecule has law expression LE_CIPFS |
| USES | LE → A / G / PD in its formula | LE_CIPFS uses G_PAY_FORM |
| MEMBER | G → its members | G_PAY_FORM → A_PAY_NONMONEY |
| FEEDS | LE → the product it issues | LE_CIPFS → PD_CONSIDERATION_INCLUDED |
| READS | A → F | A_PAY_NONMONEY reads F_PAYMENT_FORM |
| GROUPED_IN | A → TSG | A_PAY_NONMONEY is in TSG01_03 |
| IN_GOVERNANCE_MASTER | MKP → governance master | used by governance master TSG01_03 |

Example queries:

```sql
SELECT element_type, element_id, concept FROM v_molecule_elements
WHERE molecule = 'CGSTA17_S2_C31_SCa#TEXT' ORDER BY element_type, element_id;

SELECT parent_id, relation, child_id FROM v_element_links WHERE molecule = 'CGSTA17_S2_C31_SCa#TEXT';
```

The governance master Excel report also has a **Molecules** sheet with the same table for every law in the governance master, including each element's id inside the governance master.
