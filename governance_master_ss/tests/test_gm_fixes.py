"""The governance master fixes of 31 Oct 2026 (no database needed)."""
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from governance_master_ss import pack  # noqa: E402
from governance_master_ss.build import _clause_parent, build, value_aliases  # noqa: E402
from test_gm_build import CFG, SPINE, _rows, snapshot  # noqa: E402


def test_clause_parent():
    assert _clause_parent("CGSTR17_R27_Ca") == "CGSTR17_R27"
    assert _clause_parent("CGSTA17_S2_C31_SCb") == "CGSTA17_S2_C31"
    assert _clause_parent("CGSTA17_SCHI_EN2") == ""


def test_value_alias_maps_non_money_to_the_spine_fact():
    assert value_aliases("F_SUPPLY_CONSIDERATION = 'NON_MONEY'", CFG) == "F_NMC = TRUE"
    assert value_aliases("F_SUPPLY_CONSIDERATION IN ['MONEY', 'NON_MONEY']", CFG) == "(F_CA > 0 OR F_NMC = TRUE)"


def test_clauses_of_one_provision_share_one_step():
    b = build(snapshot(), "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "v", depth=3)
    step = next(c for c in b.rulebook["compounds"] if c["id"] == "C_S2_C31")
    assert {"LE_CIPFS", "LE_MVAF"} <= set(step["includes"])
    assert step["question"].startswith("Section 2(31)")


def test_definition_given_by_the_spine_pulls_nothing_else():
    snap = snapshot()
    snap.definitions = [{"term": "supply", "defined_in": "CGSTA17_S7_SS1"}]
    snap.akp += _rows("CGSTA17", "CGSTA17_S7_SS1_Cc#TEXT", "M_SISIA", [
        ("MKP", "M_SISIA", "NA", 'citation: "Section 7(1)(c)"; citation_key: CGSTA17_S7_SS1_Cc; knowledge_type: SCOPE'),
        ("LE", "LE_SISIA", "NA", 'outcome: SCHEDULE_I; expr: "A_SCHED1"'),
        ("A", "A_SCHED1", "Schedule I", "statement: \"The activity is in Schedule I\"; order: HIGH; facts: [F_ACTIVITY_DESCRIPTION]; "
                                        "interpretation: \"Is it in Schedule I?\""),
    ])
    b = build(snap, "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "v", depth=1)
    assert "CGSTA17_S7_SS1_Cc#TEXT" not in b.included          # 7(1)(c) is not pulled in through "supply"


def test_order_that_is_not_low_or_high_is_fixed_and_checked():
    snap = snapshot()
    for r in snap.akp:
        if r["element"] == "A_OMVA":
            r["results"] = r["results"].replace("order: LOW", "order: 2")
    b = build(snap, "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "v", depth=3)
    assert b.rulebook["predicates"]["A_OMVA"]["order"] == "LOW"
    assert any("A_OMVA" in w and "not LOW / HIGH" in w for w in b.report["warnings"])
    bad = copy.deepcopy(b.rulebook)
    bad["predicates"]["A_OMVA"]["order"] = "1"
    assert any("order must be LOW or HIGH" in e for e in pack.check_pack(bad))


def test_reference_to_a_schedule_uses_its_steps():
    snap = snapshot()
    sched = "CGSTA17_SCHI_EN1#TEXT"
    snap.akp += _rows("CGSTA17", sched, "M_PTOBA", [
        ("MKP", "M_PTOBA", "NA", 'citation: "Schedule I, paragraph 1"; citation_key: CGSTA17_SCHI_EN1; knowledge_type: DEEMING'),
        ("LE", "LE_PTOBA", "NA", 'outcome: TREATED_AS_SUPPLY; expr: "A_PERM"'),
        ("A", "A_PERM", "Permanent transfer", "statement: \"Business assets are permanently transferred\"; order: LOW; "
                                              "facts: [F_PAYMENT_FORM]; rule: \"F_PAYMENT_FORM = 'NON_MONEY'\""),
    ])
    r27 = "CGSTR17_R27#TEXT"
    for r in snap.akp:
        if r["source_mkp"] == r27 and r["element"] == "A_OMVA":
            r["results"] += "; reference_keys: [CGSTA17_SCHI]"
    snap.gkp.append({"source_mkp": sched, "akp_element": "A_PERM", "akp_type": "A", "tg_id": "TG01",
                     "tsg_id": "TSG01_03", "status": "APPROVED"})
    b = build(snap, "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "v", depth=1)
    assert "A_OMVA" not in b.rulebook["predicates"]                                   # no question asked again
    assert "PD_PTOBA" in b.rulebook["outcomes"]["LE_V27A"]["expr"]                   # Schedule I's step answers it
    ids = [c["id"] for c in b.rulebook["compounds"]]
    assert ids.index("C_PTOBA") < ids.index("C_VSCNWM")                               # and runs first
    assert pack.check_pack(b.rulebook) == []


def test_spine_partner_is_recorded():
    b = build(snapshot(), "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "v", depth=3)
    assert {"spine_id": "A_AIAB", "atom": "A_BARTER"}.items() <= next(
        p for p in b.report["spine_partners"] if p["atom"] == "A_BARTER").items()
