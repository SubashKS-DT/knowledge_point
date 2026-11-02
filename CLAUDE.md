# CLAUDE.md — DARP LAW ARC (GST law → knowledge points → Decision Graph Engine)

Read `README.md` and `docs/HOW_IT_WORKS.md` first. They describe the real flow, tables and prompts of this repo.
`docs/GOVERNANCE_MASTER.md` describes the governance master.

```
PDF ─► IKP ─► MKP ─► AKP (+ fact network) ─► GKP ─► GOVERNANCE MASTER ─► engine
```

## Working rules (every session)

1. **Read before you write:** this file, the docs, `main.py`, the step engine and its `<step>_ss/` folder, the prompts in `Prompt/`, `permission.json`, `Config/settings.json` and `Config/gst_taxonomy.json`. Report the gaps and wait for approval before any schema change.
2. **Existing scripts and prompts are the baseline.** Change only what is required, or what fixes a real defect. No unwanted tables, columns or features. Ask first if you think something extra is needed.
3. **Layout:** one `<step>_engine.py` plus a `<step>_ss/` folder with `tests/` per step (ikp, mkp, akp, gkp, governance_master). Names use IKP / MKP / AKP / GKP / governance master, never "pass".
4. **`main.py` changes only with approval.** Show the new or changed function and where it goes.
5. **No hidden defaults.** Day-to-day switches and the LLM + model per job are in `permission.json`; fixed paths, tables, prompts and thresholds are in `Config/settings.json` (both read by `kp_settings.py`). A missing key stops the run with its name.
6. **Prompts keep their flow:** the same header, row types and step order. Changing a prompt changes the raw LLM-answer cache keys of that step. Batch prompt edits, and log them at the top of the prompt file.
7. **Every model answer passes a Python gate before it is saved.** Write tests for parsers, gates, versioning and resume. Tests that need PostgreSQL must `pytest.skip` when the DB is down. Prefer tests that run without the DB.
8. **Versioning everywhere:** insert only, never `DELETE` or overwrite content. A new version only when the content is different; then version n+1 and `is_latest=false` on older rows. Readers always filter `is_latest`.
9. **Database first:** MKP, AKP and GKP reuse what is saved when their input did not change, and log why the LLM was called when it did.
10. **Never invent** legal text, IDs, citations, facts, or the engine's rulebook format. When something is unknown, stop and ask.
11. **Never put secrets in code or tests.** Credentials live only in `Credentials/` (`.env`, `service_crendential.json`, `claude_crendential.json`).

## Prompts actually used

| Step | Prompt files (in `Prompt/`) |
|---|---|
| IKP | `law_arc_ikp_map_prompt.txt` (LLM assist, only for chapters that fail the gate) |
| MKP | `law_arc_mkp_prompt.txt`, `law_arc_mkp_judge_prompt.txt` |
| AKP | `law_arc_akp_prompt.txt`, `law_arc_akp_judge_prompt.txt`, `law_arc_akp_comparison_prompt.txt`, `law_arc_akp_same_prompt.txt` |
| GKP | `law_arc_gkp_prompt.txt` |

## IDs and words that must not be mixed up

- **`G_`** is a logic group inside one MKP (AKP).
- **`TG01`… / `TSG01_01`…** are taxonomy groups (TG) and sub-groups (TSG) from GKP; columns `tg_id` and `tsg_id`.
- **Facts (`F_<ENTITY>_<ATTRIBUTE>`)** are inputs, never conclusions. One fact keeps one id across all laws. `akp_atom_fact` is the fact network.
- **Two kinds of version:**
  - **Run version** (`version_no`, `mkp_version`, `akp_version`, `gkp_version`) is our own output.
  - **Law version** (`akp_time_fact.effective_from`, AMENDMENT MKPs) is when the law itself changed.

## Governance master

See `docs/GOVERNANCE_MASTER.md`.
- One governance master per TSG, in `Governance_Master/<TG>/<TSG>/` (outside `Output/`). Call it a "governance master", never "rulebook". The file inside stays `rulebook.yaml`, because the engine requires that name.
- It uses no LLM and no engine. The governance master is checked in Python (`governance_master_ss/pack.py`).
- The main table is `governance_master`, with one row per element; history goes to `governance_master_history`.
- It runs right after GKP of each document, with no user input, for every TSG with laws (spine-only TSGs too).
- Mapping decisions (fact aliases, tiers, spine coverage) live in `Config/governance_master_map.json`, which people review. Never guess a mapping in code.
