# DARP LAW ARC: GST law → knowledge points → Decision Graph Engine

LAW ARC turns GST law PDFs (Acts, Rules, Notifications, Circulars) into knowledge points, and keeps one
**governance master** per TSG (taxonomy sub-group) for the Decision Graph Engine.

```
PDF ─► IKP ─► MKP ─► AKP ─► GKP ─► GOVERNANCE MASTER ─► Decision Graph Engine
     structure  molecules  atoms+facts  TG / TSG      one per TSG
```

| Step | What it makes | Main table(s) |
|---|---|---|
| IKP | The structure of the book: every chapter, section, sub-section, clause, proviso and explanation, with exact text, pages and PDF position | `ikp`, `ikp_nodes` |
| MKP | One molecule per provision: the exact legal text with its full meaning, plus source phrases (SP) | `mkp` |
| AKP | The molecule split into atoms, logic groups, law expressions, outcomes, catalysts, test cases **and facts** | `akp`, `akp_fact`, `akp_atom_fact`, `akp_time_fact` |
| GKP | Every atom placed in the GST taxonomy (TG01–TG18, TSG01_01 …) | `gkp` |
| GOVERNANCE MASTER | One `rulebook.yaml` per TSG: its laws + the laws they point to + the spine | `governance_master` (+ `_history`) and `Governance_Master/<TG>/<TSG>/rulebook.yaml` |

## Quick start

1. **Credentials.** Keep your own `Credentials/` folder (not shipped): `.env` (`LEGAL_DB_NAME`, `LEGAL_DB_USER`,
   `LEGAL_DB_PASSWORD`, optional `LEGAL_DB_HOST` / `LEGAL_DB_PORT`), `service_crendential.json` (Gemini, Vertex
   embeddings), `claude_crendential.json` (Claude).
2. **Database.** PostgreSQL with `pgvector`. Tables are created by the steps. Old `pass0_` … `pass_4_law_pack` tables
   are renamed automatically on the first run (`db_migrate.py`), keeping every row.
3. **Python packages:** `pymupdf`, `psycopg2-binary`, `python-dotenv`, `openpyxl`, `xlsxwriter`, `pandas`,
   `google-genai`, `anthropic`, `lark`, `pyyaml`, `pytest`.
4. **Law PDFs:** `Law/Acts`, `Law/Rules`, `Law/Notifications`, `Law/Circulars`.
5. **Choose** in `permission.json`: `run`, `use_cache`, `read` (which kinds of law), `scope`, `governance_master`,
   `llm` (LLM + model per job).
6. **Run:** `python main.py`
7. **Test:** `python -m pytest -q` (database and PDF tests skip themselves when those are not available).

## Documentation

| File | Read it for |
|---|---|
| `docs/HOW_IT_WORKS.md` | The full flow, settings, cache and versions, tables and outputs |
| `docs/GOVERNANCE_MASTER.md` | Governance masters: how they are built and updated, the main table, tests |
| `docs/IKP_DETAILS.md` | IKP internals and results on the sample PDFs |
| `CLAUDE.md` | Working rules for Claude Code in this repo |
