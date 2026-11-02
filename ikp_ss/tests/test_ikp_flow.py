"""IKP flow tests: LLM assist safety, block re-read from the PDF, citation lookup.
Run: python -m pytest -q ikp_ss/tests
"""
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ikp_ss import llm_assist as L  # noqa: E402
from ikp_ss.build import analyse, chapter_groups, group_records, re_enrich  # noqa: E402
from ikp_ss.gates import QualityReport, evaluate_group  # noqa: E402
from ikp_ss.reader import IkpIndex  # noqa: E402

LAW = ROOT / "Law"


def _pdf(name):
    """The PDF in Law/Acts, Law/Rules, ... (or Law/ itself); skip when it is not there."""
    for f in [LAW / name, *LAW.glob(f"*/{name}")]:
        if f.is_file():
            return f
    pytest.skip(f"{name} not in Law/")
PROMPT = (ROOT / "Prompt" / "law_arc_ikp_map_prompt.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules():
    return analyse(_pdf("CGST-Rules-2017-Part-A.pdf"), "CGSTR17", "RULES")


def _group(a, key):
    return next(g for g in chapter_groups(a.parsed) if g.key == key)


def _run(a, key, response, force_fail=True):
    """Run the LLM assist with a fake model. force_fail makes the 'before' gate FAIL so a good answer wins."""
    cache = {}
    real_gate = lambda an, g: evaluate_group(an.parsed, g)
    calls = {"n": 0}

    def gate(an, g):
        q = real_gate(an, g)
        if force_fail and calls["n"] == 0:
            calls["n"] += 1
            return QualityReport("FAIL", q.metrics, ["forced for test"], [])
        return q
    return L.assist_group(a, _group(a, key), PROMPT, lambda p: response(p), gate, _group, re_enrich,
                          cache.get, lambda k, r: cache.__setitem__(k, r), "TEST:fake", ["forced"])


def test_llm_correct_tags_rebuild_identical_chapter(rules):
    a = copy.deepcopy(rules)
    g = _group(a, "CGSTR17_CHVI")
    before = {k: (a.parsed.nodes[k].parent, a.parsed.own_text(k)) for k in g.keys}
    answer = json.dumps({"starts": L.python_draft(a, g)})
    new_group, rep, q = _run(a, "CGSTR17_CHVI", lambda p: answer)
    assert rep.accepted == len(g.keys) - 1 and not rep.rejected
    after = {k: (a.parsed.nodes[k].parent, a.parsed.own_text(k)) for k in new_group.keys}
    assert before == after                      # same keys, same parents, same text
    assert "the consigner may issue" in a.parsed.own_text("CGSTR17_R55_SR1_CL1")


def test_llm_wrong_tags_are_rejected(rules):
    a = copy.deepcopy(rules)
    g = _group(a, "CGSTR17_CHVI")
    draft = L.python_draft(a, g)
    bad = copy.deepcopy(draft)
    bad[3]["number"] = "zz"                     # label not printed there
    bad[5]["level"] = "PROVISO"                 # line does not start with 'Provided'
    bad.insert(0, {"line": "L1", "at": 0, "level": "RULE", "number": "1", "parent": None})  # outside chapter
    tags, rejected = L.validate(a, g, {"starts": bad})
    assert len(rejected) >= 3
    assert all("does not start with" in r or "outside chapter" in r or "parent" in r for r in rejected)


def test_llm_failure_keeps_python(rules):
    a = copy.deepcopy(rules)
    before = a.parsed.own_text("CGSTR17_R55_SR1_Cd")

    def broken(prompt):
        raise RuntimeError("401 invalid x-api-key")
    new_group, rep, q = _run(a, "CGSTR17_CHVI", broken)
    assert rep.error and a.parsed.own_text("CGSTR17_R55_SR1_Cd") == before


def test_llm_never_changes_text(rules):
    a = copy.deepcopy(rules)
    g = _group(a, "CGSTR17_CHVI")
    stream_before = a.parsed.stream
    answer = json.dumps({"starts": L.python_draft(a, g)[:20]})   # partial answer
    _run(a, "CGSTR17_CHVI", lambda p: answer)
    assert a.parsed.stream == stream_before


def test_prompt_contains_numbered_lines(rules):
    g = _group(rules, "CGSTR17_CHVI")
    prompt = L.build_prompt(PROMPT, rules, g, [])
    assert "=== LINES ===" in prompt and "| 55. Transportation of goods" in prompt


def test_blocks_and_reread_from_pdf(rules, tmp_path):
    texts = []
    for g in chapter_groups(rules.parsed):
        texts += group_records(rules, g)[1]
    idx = IkpIndex("CGSTR17", texts)
    for key in ("CGSTR17_R55_SR1_Cd", "CGSTR17_R55_SR1_CL1", "CGSTR17_R36_SR4", "CGSTR17_R7_TBL1_ROW2"):
        r = idx.read_from_pdf(_pdf("CGST-Rules-2017-Part-A.pdf"), key)
        assert r["matches_index"], (key, r)
    assert idx.get("CGSTR17_R55_SR1_Cd")["blocks"][0]["page"] == 65


def test_citation_lookup(rules):
    texts = []
    for g in chapter_groups(rules.parsed):
        texts += group_records(rules, g)[1]
    idx = IkpIndex("CGSTR17", texts)
    assert idx.resolve("rule 55(1)(d)") == "CGSTR17_R55_SR1_Cd"
    assert idx.resolve("rule 36(4)") == "CGSTR17_R36_SR4"
    assert idx.resolve("55A") == "CGSTR17_R55A"
    ctx = idx.context("CGSTR17_R55_SR1_Cd")
    assert ctx["lead_ins"][-1]["element"] == "CGSTR17_R55_SR1"
    assert ctx["qualifiers_after"][0]["element"] == "CGSTR17_R55_SR1_CL1"
