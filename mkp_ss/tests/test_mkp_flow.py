"""MKP flow tests: fake Gemini / Claude / judge answering in the real pipe-table formats, local PostgreSQL.
Needs the IKP index of CGSTA17 in gst_legal_validation_new (run IKP first).
Run: python -m pytest -q mkp_ss/tests
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import ikp_engine as p0  # noqa: E402
import mkp_engine as p1  # noqa: E402
from transport import get_field, mkp_bundles  # noqa: E402

TABLE = "mkp_test"


def _q(text: str) -> str:
    return '"' + text.replace(";", ",").replace("|", "/").replace('"', '\\"') + '"'


def _refs(prompt: str):
    """Reference lines of the PRE-GATE REPORT (judge) -> [(element, kind, text)]."""
    block = prompt.split("PYTHON REFERENCE TEXT", 1)[1].split("\nMODEL_1", 1)[0]
    return re.findall(r"^  (\S+#\S+) \[(\w+)\] (.*)$", block, re.M)


class Fake:
    """MODEL_1 drops a word in one clause, MODEL_2 is right; the judge builds FINAL from the reference text.
    judge_mode: GOOD | BAD_ONCE (first FINAL drops a word -> gate -> retry) | ALWAYS_BAD"""

    def __init__(self, drafter, judge_mode="GOOD"):
        self.drafter, self.judge_mode = drafter, judge_mode
        self.calls = {"GEMINI": 0, "CLAUDE": 0, "JUDGE": 0}

    def _rows(self, cluster, items, model):
        rows = []
        for n, (el, kind, text) in enumerate(items, 1):
            ck, part = el.split("#", 1)
            if model == 1 and kind == "TEXT" and n == 2:
                text = " ".join(text.split()[:-3] + text.split()[-2:])        # drops one word
            kt = {"HEADING": "HEADING", "AMENDMENT": "AMENDMENT"}.get(kind, "OBLIGATION")
            res = (f'concept: "Knowledge Point {n} Text"; citation: "{ck}"; citation_key: {ck}; part: {part}; '
                   f'knowledge_type: {kt}; topic: "T"; subtopic: "S"; cluster_role: MEMBER; parent: NA; '
                   f'text: {_q(text)}; match: NEW; existing: NA; match_reason: "first load"')
            words = text.replace(";", ",").replace("|", "/").split()
            half = max(1, len(words) // 2)
            sps = [" ".join(words[:half]), " ".join(words[half:])] if len(words) > 1 else [" ".join(words)]
            rows.append(("MKP", el, res))
            for i, s in enumerate([s for s in sps if s], 1):
                rows.append(("SP", el, f"sp: SP{i}; text: {_q(s)}"))
        return ([("BATCH", "DOC", 'instrument: "X"; cluster_name: "N"; question: "NA"')] + rows +
                [("SUM", "DOC", f"mkp_count: {sum(1 for r in rows if r[0] == 'MKP')}")])

    def _items(self, prompt):
        card = json.loads(re.search(r"CLUSTER CARD\n(\{.*\})", prompt).group(1))
        expected = json.loads(re.search(r"EXPECTED PROVISION LIST\n(\[.*\])", prompt).group(1))
        allowed = set(expected) | {s["owner"] for s in card["shared_amendments"]}
        unit = card["id"][2:]
        items = []
        for d in self.drafter.draft(unit):
            if d.citation_key in allowed and (d.part.startswith("AMD") or d.citation_key in expected):
                items.append((f"{d.citation_key}#{'TEXT' if d.part == 'HEAD' else d.part}", d.kind, d.text))
        return card["id"], items

    def __call__(self, provider, model, prompt):
        if "===== PRE-GATE REPORT =====" in prompt:
            self.calls["JUDGE"] += 1
            cluster = json.loads(re.search(r"CLUSTER CARD: (\{.*\})", prompt).group(1))["id"]
            items = _refs(prompt)
            bad = self.judge_mode == "ALWAYS_BAD" or (self.judge_mode == "BAD_ONCE" and "FAILED THE PYTHON GATE" not in prompt)
            if bad:
                first = next(i for i, (e, k, t) in enumerate(items) if k == "TEXT")
                items = [(e, k, " ".join(t.split()[1:]) if i == first else t) for i, (e, k, t) in enumerate(items)]
            body = "\n".join(f"{cluster}|{t}|{e}|{r}|{r}|{r}|score: 0.95; band: HIGH; outcome: AGREE"
                             for t, e, r in self._rows(cluster, items, 2))
            return "CLUSTER|TYPE|ELEMENT|MODEL_1|MODEL_2|FINAL|CONFIDENCE\n" + body
        self.calls[provider] += 1
        cluster, items = self._items(prompt)
        rows = self._rows(cluster, items, 1 if provider == "GEMINI" else 2)
        return "CLUSTER|TYPE|ELEMENT|RESULTS\n" + "\n".join(f"{cluster}|{t}|{e}|{r}" for t, e, r in rows)


def fake_embed(text):
    v = [0.0] * 8
    for w in text.lower().split():
        v[hash(w) % 8] += 1.0
    return v


@pytest.fixture(scope="module")
def env():
    permission = __import__("kp_settings").load()
    paths = p1.build_paths(permission)
    try:
        conn = p0.get_connection(paths)
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    with conn.cursor() as cur:
        for t in ("", "_cache", "_llm_cache"):
            cur.execute(f"DROP TABLE IF EXISTS public.{TABLE}{t}")
    conn.commit()
    settings = p1.validate_mkp_settings({**permission["mkp"], "enabled": True, "ask_user_input": False, "table": TABLE,
                                           "excel_file": "mkp_test.xlsx", "verify_blocks": True})
    doc = next(dict(v, document_key=k) for k, v in permission["documents"].items() if v["code"].upper() == "CGSTA17")
    run = p1.MkpRun(doc, permission, paths, settings, conn, embed=fake_embed)
    yield permission, paths, settings, conn, doc, run.drafter
    conn.close()


def _rows(conn, where="", *args):
    with conn.cursor() as cur:
        cur.execute(f"SELECT cluster, type, element, results, embedding IS NOT NULL, version_id, mkp_id "
                    f"FROM public.{TABLE} {where} ORDER BY ctid", args)
        return cur.fetchall()


def _go(env, roots, fake, **over):
    permission, paths, settings, conn, doc, _ = env
    return p1.run_mkp_document(doc, permission, paths, dict(settings, **over), conn, scope_roots=roots,
                                 fake_llm=fake, embed=fake_embed)


def test_section_7_saved_in_old_format(env):
    fake = Fake(env[5])
    t = _go(env, ["CGSTA17_S7"], fake)
    assert t["completed"] == 1 and t["failed"] == 0
    assert fake.calls == {"GEMINI": 1, "CLAUDE": 1, "JUDGE": 1}
    rows = _rows(env[3], "WHERE cluster='C_CGSTA17_S7'")
    types = {r[1] for r in rows}
    assert {"BATCH", "MKP", "SP", "SUM"} <= types
    mkps = [r for r in rows if r[1] == "MKP"]
    assert all(r[4] for r in mkps) and all(r[5].startswith("V_") for r in rows) and all(r[6] for r in mkps)
    el = {r[2]: r[3] for r in mkps}
    assert get_field({"RESULTS": el["CGSTA17_S7_SS1_Cb#TEXT"]}, "parent") == "CGSTA17_S7_SS1"   # from IKP
    assert "for a consideration whether or not" in get_field({"RESULTS": el["CGSTA17_S7_SS1_Cb#TEXT"]}, "text")
    # AKP reads these rows with transport.mkp_bundles - must give one bundle per MKP with phrases
    bundles = mkp_bundles([{"TYPE": r[1], "ELEMENT": r[2], "RESULTS": r[3]} for r in rows])
    assert len(bundles) == len(mkps) and all(b["sp_rows"] for b in bundles)


def test_rerun_is_skipped_without_llm(env):
    fake = Fake(env[5])
    t = _go(env, ["CGSTA17_S7"], fake)
    assert t["cached"] == 1 and sum(fake.calls.values()) == 0


def test_judge_retry_after_fidelity_error(env):
    fake = Fake(env[5], "BAD_ONCE")
    t = _go(env, ["CGSTA17_S9"], fake)
    assert t["completed"] == 1 and fake.calls["JUDGE"] == 2


def test_always_wrong_text_is_never_saved_and_unit_is_split(env):
    fake = Fake(env[5], "ALWAYS_BAD")
    t = _go(env, ["CGSTA17_S10"], fake, use_cache=False)
    assert t["recovered"] >= 1 and t["failed"] >= 1 and t["completed"] == 0
    assert not _rows(env[3], "WHERE cluster='C_CGSTA17_S10' AND type='MKP' AND element LIKE 'CGSTA17_S10_SS1%%'")
    details = (env[1]["output_dir"] / "mkp_failed_details.txt").read_text(encoding="utf-8")
    assert "FIDELITY" in details


def test_heading_and_text_on_one_node(env):
    t = _go(env, ["CGSTA17_S21"], Fake(env[5]))
    assert t["completed"] == 1
    els = {r[2] for r in _rows(env[3], "WHERE cluster='C_CGSTA17_S21' AND type='MKP'")}
    assert {"CGSTA17_S21#TEXT", "CGSTA17_S21#HEAD"} <= els


def test_narrower_scope_replaces_rows_once(env):
    _go(env, ["CGSTA17_S16"], Fake(env[5]))
    _go(env, ["CGSTA17_S16_SS2"], Fake(env[5]))
    rows = _rows(env[3], "WHERE type='MKP' AND element='CGSTA17_S16_SS2_Ca#TEXT' AND is_latest")
    assert len(rows) == 1 and rows[0][0] == "C_CGSTA17_S16"


def test_rerun_with_same_answer_keeps_the_version(env):
    """use_cache=false asks the LLM again; the same MKP text is not saved as a new version."""
    _go(env, ["CGSTA17_S7"], Fake(env[5]), use_cache=False)
    with env[3].cursor() as cur:
        cur.execute(f"SELECT mkp_version, is_latest FROM public.{TABLE} WHERE type='MKP' "
                    "AND element='CGSTA17_S7_SS1_Ca#TEXT' ORDER BY mkp_version")
        got = cur.fetchall()
    assert got == [(1, True)]


def test_akp_version_lookup_finds_scope(env):
    p2 = pytest.importorskip("akp_engine", reason="AKP engine not in this folder")
    from psycopg2 import sql  # noqa: F401
    conn = env[3]
    orig = p2._table
    p2._table = lambda name: __import__("psycopg2").sql.SQL("public.{}").format(__import__("psycopg2").sql.Identifier(name))
    try:
        version = p2._version(conn, "CGSTA17", TABLE, "CGSTA17_S7")
        out = p2._read_bundles(conn, "CGSTA17", TABLE, version, "CGSTA17_S7", "")
    finally:
        p2._table = orig
    assert version.startswith("V_") and len(out) >= 10
    assert out[0][0]["id"] == "C_CGSTA17_S7" and out[0][0]["name"]


def test_already_done_unit_is_still_handed_to_pass_2(env):
    """Re-entering a law MKP already has: no model call, but AKP / 3 still get the unit (they check their own cache)."""
    permission, paths, settings, conn, doc, _ = env
    _go(env, ["CGSTA17_S7"], Fake(env[5]))                       # make sure it is done once
    handed = []
    fake = Fake(env[5])
    t = p1.run_mkp_document(doc, permission, paths, dict(settings, use_cache=True), conn, scope_roots=["CGSTA17_S7"],
                              fake_llm=fake, embed=fake_embed, on_scope_complete=handed.append)
    assert t["cached"] == 1 and fake.calls == {"GEMINI": 0, "CLAUDE": 0, "JUDGE": 0}
    assert [h["scope_key"] for h in handed] == ["CGSTA17_S7"] and handed[0]["mkp_status"] == "CACHED"
