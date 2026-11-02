# IKP — the structure of a GST law document

This package contains **IKP only**. MKP, 2 and 3 are not included and were not changed.

## Files

```
ikp_engine.py        entry point (main.py calls run_ikp_pipeline as before)
ikp_ss/                     IKP supporting scripts
  layout.py      read PDF: remove headers/footers/contents page, separate footnotes, margin notes, footnote markers
  structure.py   Python splitter: chapter > section/rule > sub-section > clause > sub-clause > proviso /
                 explanation / closing text / table rows / annexures / Removal of Difficulty orders
  enrich.py      status (inserted/substituted/omitted) + amendment dates from footnotes, cross-references, definitions
  citations.py   reads "section 16(2)(c)", "sub-sections (1), (1A) and (2)", "sections 73 to 76"
  gates.py       quality check per chapter and per document
  llm_assist.py  LLM step (only for a chapter that fails the check)
  build.py       per-chapter records: text, PDF blocks (page + box + line ids), anchors
  store.py       PostgreSQL (versioned) + Excel
  reader.py      what MKP will use: get / resolve citation / context / read_from_pdf
  tests/         18 tests  ->  python -m pytest -q ikp_ss/tests
Prompt/law_arc_ikp_map_prompt.txt          new prompt for the LLM step (old one kept as *_v1_old.txt)
permission_ikp_block.json                  the "ikp" block to paste into permission.json
```

Copy these into your project folder. `main.py` and `llm_runtime.py` are unchanged; IKP uses them as they are.

## Setup

1. `Credentials/.env`: `LEGAL_DB_NAME=gst_legal_validation_new`
2. Create the database once with `CREATE DATABASE gst_legal_validation_new;`. IKP creates its own tables.
3. Replace the `"ikp"` block in `permission.json` with the one in `permission_ikp_block.json`.
4. **Keep `mkp.enabled=false` for now.** The current MKP still reads the old IKP tables. It will be switched to `ikp_ss/reader.py` in the MKP step.

## Flow for one document

1. Read the PDF and remove page noise. Footnotes become amendment records.
2. Python finds the chapters and their page ranges, then splits each one down to sub-clauses, provisos, closing text and table rows.
3. For each chapter:
   - Run the quality check.
   - If it **FAILS**, the LLM tags line starts and Python checks them. The LLM version is kept only if it scores better.
   - Save the chapter to the DB as soon as it is done (checkpoint).
4. Run the document-level check. The version is marked COMPLETE and becomes the latest.

## Switches

| setting | effect |
|---|---|
| `enabled=false` | The PDF is not read; the latest saved version is used. |
| `use_cache=true` | Same PDF already COMPLETE → reused. An unfinished run of the same PDF → resumes from the next unsaved chapter. Otherwise → new version. |
| `use_cache=false` | Always reads the PDF again as a **new version**. Old versions are kept. |
| `llm.mode` | `ON_FAIL` (default): LLM only for failed chapters. `OFF`: never. `ALWAYS`: every chapter. |
| `llm.provider` / `llm.model` | `CLAUDE` or `GEMINI`, using the credentials in `Credentials/`. |
| `llm.chapters` | e.g. `["CGSTR17_CHIII"]` sends only these chapters to the LLM (to re-check one chapter). |
| `fail_on_gate` | A document whose check FAILS is saved but not marked as latest. |

## DB tables (`gst_legal_validation_new`)

| table | content |
|---|---|
| `ikp_versions` | doc, **version_no**, pdf_sha256, **law_as_on** (e.g. "31.08.2021") / law_as_on_date, status RUNNING/COMPLETE/FAILED, **is_latest**, quality, started_at, completed_at |
| `ikp_chapters` | one row per saved chapter: status, method PYTHON / LLM_ASSIST, issues, LLM report (the resume checkpoint) |
| `ikp` | the five columns DOC, TYPE, ELEMENT, PAGE_NUMBER, RESULTS + version_no, chapter, **indexed_at**, **is_latest** |
| `ikp_nodes` | one row per node: own_text, full_text, **blocks** (page, bbox, PDF line ids), start/end anchors, parent, children, status, amendments, xrefs, flags |
| `ikp_llm_cache` | LLM answers by prompt hash, so a re-run makes no repeat calls |

Read the latest version with `WHERE doc='CGSTA17' AND is_latest`.

## How MKP will get the exact block

```python
from ikp_ss.reader import IkpIndex
idx = IkpIndex.load("CGSTA17", paths, settings)      # latest version
key = idx.resolve("section 7(1)(a)")                   # -> CGSTA17_S7_SS1_Ca
idx.text(key)                                          # exact stored wording
idx.context(key)                                       # + parent lead-ins, closing text, provisos, amendments, xrefs
idx.read_from_pdf("Law/CGST-Act.pdf", key)             # cut the same block out of the PDF again (check)
```

## Results on the 4 PDFs

| document | check | structure | re-cut from PDF = stored text |
|---|---|---|---|
| CGST Act | WARN (1 footnote marker without text) | 21 chapters, sections 1–174, 3 schedules, 7 Removal of Difficulty orders | 1838/1838 |
| IGST Act | PASS | 9 chapters, sections 1–25 | 310/311 |
| CGST Rules | WARN (6 markers to review) | 19 chapters, rules 1–162 incl. all A/B/C rules, tables as rows | 1877/1877 |
| Circular 10/2017 | PASS | subject, paragraphs 1–7, signature | 10/10 |

All four documents run in about 25 seconds. No chapter failed, so no LLM call was needed.
The LLM step was tested with simulated answers (correct, wrong and failing). A live call could not be
tested because the Claude key in the uploaded Credentials folder is no longer valid (401).

## Still open

- **Rate notifications, amendment notifications and Forms:** the parser and the LLM fallback are ready for them, but they need real sample PDFs to test.
- **Table cells:** rows are separated, but the text inside a row follows PDF reading order, not column order.
