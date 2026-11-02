"""IKP regression tests - run with:  python -m pytest -q tests/test_ikp_regression.py

Every case here was a real failure of the old LLM-based IKP. Add a case whenever a new
section/rule misbehaves, then fix the parser until all tests pass again.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ikp_ss.build import analyse, document_quality  # noqa: E402


def build_index(pdf, code, kind):
    a = analyse(pdf, code, kind)
    a.quality = document_quality(a)
    return a

LAW = ROOT / "Law"


def _pdf(name):
    """The PDF in Law/Acts, Law/Rules, ... (or Law/ itself); skip when it is not there."""
    for f in [LAW / name, *LAW.glob(f"*/{name}")]:
        if f.is_file():
            return f
    pytest.skip(f"{name} not in Law/")


@pytest.fixture(scope="module")
def act():
    return build_index(_pdf("CGST-Act.pdf"), "CGSTA17", "ACT")


@pytest.fixture(scope="module")
def rules():
    return build_index(_pdf("CGST-Rules-2017-Part-A.pdf"), "CGSTR17", "RULES")


@pytest.fixture(scope="module")
def igst():
    return build_index(_pdf("IGST-Act.pdf"), "IGSTA17", "ACT")


def units(result, level):
    return [n.number for k, n in result.parsed.nodes.items() if n.level == level]


def own(result, key):
    return result.parsed.own_text(key)


def test_act_chapters_and_sections(act):
    assert len([n for n in act.parsed.nodes.values() if n.level == "CHAPTER"]) == 21
    secs = units(act, "SECTION")
    assert secs[0] == "1" and secs[-1] == "174"
    for inserted in ("31A", "49A", "49B", "53A", "168A"):
        assert inserted in secs
    assert act.quality.metrics["text_coverage_pct"] == 100.0
    assert act.quality.metrics["out_of_sequence_markers"] == 0


def test_act_section7_structure(act):
    n = act.parsed.nodes
    assert n["CGSTA17_S7"].heading == "Scope of supply"
    assert n["CGSTA17_S7"].parent == "CGSTA17_CHIII"
    assert [c.split("_")[-1] for c in n["CGSTA17_S7_SS1"].children] == ["Ca", "Cb", "Cc", "Cd"]
    assert own(act, "CGSTA17_S7_SS1_Ca").startswith("(a) all forms of supply")
    assert n["CGSTA17_S7_SS1_Cd"].status == "OMITTED"
    assert n["CGSTA17_S7_SS1A"].status == "INSERTED"
    assert own(act, "CGSTA17_S7_SS2_CL1").startswith("shall be treated neither")


def test_act_provisos_qualify_subsection(act):
    kids = act.parsed.nodes["CGSTA17_S16_SS2"].children
    assert {"CGSTA17_S16_SS2_P1", "CGSTA17_S16_SS2_P2", "CGSTA17_S16_SS2_P3"} <= set(kids)
    assert "CGSTA17_S16_SS4_P1" in act.parsed.nodes  # proviso of the last sub-section stays there


def test_act_inline_and_wrapped_headings(act):
    n = act.parsed.nodes
    assert "CGSTA17_S6_SS1" in n          # heading wraps to 2 lines before '— (1)'
    assert "CGSTA17_S79_SS1_Cc_SCi" in n  # '(c) (i) the proper officer'
    assert "CGSTA17_S142_SS2_Ca" in n     # '(2) (a) where, in pursuance'


def test_act_schedules_and_annex(act):
    n = act.parsed.nodes
    assert [k for k in n if k.startswith("CGSTA17_SCHII_EN") and k.count("_") == 2][:2] == \
        ["CGSTA17_SCHII_EN1", "CGSTA17_SCHII_EN2"]
    assert n["CGSTA17_ANX"].role == "NON_OPERATIVE"  # Removal of Difficulty Orders are not sections


def test_rules_chapters_not_collapsed(rules):
    chapters = [n for n in rules.parsed.nodes.values() if n.level == "CHAPTER"]
    assert len(chapters) == 19 and chapters[0].number == "I"


def test_rules_inserted_rules_found(rules):
    nums = units(rules, "RULE")
    for r in ("10A", "21A", "41A", "55A", "59", "61", "80", "83A", "97", "100", "138E", "142A"):
        assert r in nums, r
    assert len(nums) == len(set(nums)), "duplicate rule numbers"
    assert nums[-1] == "162"


def test_rules_r55_closing_text(rules):
    n = rules.parsed.nodes
    assert [c.split("_")[-1] for c in n["CGSTR17_R55_SR1"].children] == ["Ca", "Cb", "Cc", "Cd", "CL1"]
    assert own(rules, "CGSTR17_R55_SR1_Cd") == "(d) such other supplies as may be notified by the Board,"
    assert len(n["CGSTR17_R55_SR1_CL1"].children) == 9  # (i)..(ix) belong to the closing text


def test_rules_formula_subscripts_kept(rules):
    assert "C2 = C1- T4;" in rules.parsed.stream


def test_rules_historical_versions(rules):
    hist = [n for n in rules.parsed.nodes.values() if n.level == "HISTORICAL"]
    assert hist and all(n.parent == "CGSTR17_R138" and n.status == "HISTORICAL" for n in hist)


def test_igst_gazette_layout(igst):
    assert units(igst, "SECTION") == [str(i) for i in range(1, 26)]
    assert igst.parsed.nodes["IGSTA17_S5"].heading == "Levy and collection"  # from the margin note
    assert own(igst, "IGSTA17_S12_SS3_CL1").startswith("shall be the location")
    assert igst.quality.status == "PASS"
