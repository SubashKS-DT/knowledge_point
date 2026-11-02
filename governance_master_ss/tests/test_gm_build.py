"""GOVERNANCE MASTER tests on a realistic barter snapshot (no database needed).

The snapshot mimics latest MKP-GKP rows: s.2(31)(a) and (b), the deposit proviso, Rule 27 and s.15(4), plus the
s.7(1)(a) MKP the spine already covers. Rule 27 consumes products of s.2(31)(a), s.15(4) and the spine.
Run: python -m pytest -q governance_master_ss/tests
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from governance_master_ss import rules as R  # noqa: E402
from governance_master_ss.build import build  # noqa: E402
from governance_master_ss.source import Snapshot  # noqa: E402

CFG = json.loads((ROOT / "Config" / "governance_master_map.json").read_text(encoding="utf-8"))
SPINE = yaml.safe_load((ROOT / "Config" / "spine" / "gst_s7_spine.yaml").read_text(encoding="utf-8"))


def _rows(inst, key, mkp_id, rows):
    out = []
    for i, (kind, el, concept, results) in enumerate(rows, 1):
        out.append({"instrument": inst, "version_id": "V_TEST", "source_mkp": key, "mkp_id": mkp_id, "ordinal": i,
                    "type": kind, "element": el, "concept": concept, "results": results,
                    "akp_id": f"{key}::{el}" if kind in ("A", "A_REF") else None, "same_akp_id": None})
    return out


def snapshot() -> Snapshot:
    a = "CGSTA17_S2_C31_SCa#TEXT"
    b = "CGSTA17_S2_C31_SCb#TEXT"
    p1 = "CGSTA17_S2_C31_P1#TEXT"
    r27 = "CGSTR17_R27#TEXT"
    s15 = "CGSTA17_S15_SS4#TEXT"
    s7 = "CGSTA17_S7_SS1_Ca#TEXT"
    akp = []
    akp += _rows("CGSTA17", a, "M_CIPFS", [
        ("MKP", "M_CIPFS", "NA", 'citation: "Section 2(31)(a)"; citation_key: CGSTA17_S2_C31_SCa; part: TEXT; '
                                 'knowledge_type: DEFINITION; concept: "Consideration Includes Payment"'),
        ("LE", "LE_CIPFS", "NA", 'outcome: CONSIDERATION_INCLUDED; expr: "G_PAY_FORM AND A_SUPPLY_RELATION AND '
                                 'A_IN_RESPECT AND NOT G_GOVT_SUBSIDY"; feeds: [PD_CONSIDERATION_INCLUDED]; '
                                 'nl: "The payment is consideration for the supply"'),
        ("G", "G_PAY_FORM", "NA", 'name: "Form of payment"; logic: ANY; members: [A_PAY_MONEY, A_PAY_NONMONEY]'),
        ("G", "G_GOVT_SUBSIDY", "NA", 'logic: ANY; members: [A_SUBSIDY_CENTRAL, A_SUBSIDY_STATE]'),
        ("A", "A_SUPPLY_RELATION", "Supply Subject Relation",
         "statement: \"The payment is in relation to the supply of goods or services or both\"; order: LOW; "
         "facts: [F_SUPPLY_SUBJECT]; rule: \"F_SUPPLY_SUBJECT IN ['GOODS','SERVICES','BOTH']\"; "
         'false_if: "No supply of goods or services or both is identified"'),
        ("A", "A_PAY_MONEY", "Money Payment",
         "statement: \"The payment is in money\"; order: LOW; facts: [F_PAYMENT_FORM]; rule: \"F_PAYMENT_FORM = 'MONEY'\""),
        ("A", "A_PAY_NONMONEY", "Non Money Payment",
         "statement: \"The payment is otherwise than in money\"; order: LOW; facts: [F_PAYMENT_FORM]; "
         "rule: \"F_PAYMENT_FORM == \\\"NON_MONEY\\\"\""),
        ("A", "A_IN_RESPECT", "Payment In Respect",
         "statement: \"The payment is in respect of the supply\"; order: LOW; facts: [F_PAYMENT_SUPPLY_LINK]; "
         "rule: \"F_PAYMENT_SUPPLY_LINK = 'TRUE'\""),
        ("A", "A_SUBSIDY_CENTRAL", "Central Government Subsidy",
         "statement: \"The amount is a subsidy given by the Central Government\"; order: LOW; "
         "facts: [F_PAYMENT_SUBSIDY_STATUS, F_PAYMENT_PAYER]; "
         "rule: \"F_PAYMENT_SUBSIDY_STATUS = 'TRUE' AND F_PAYMENT_PAYER = 'CENTRAL_GOVERNMENT'\""),
        ("A", "A_SUBSIDY_STATE", "State Government Subsidy",
         "statement: \"The amount is a subsidy given by a State Government\"; order: LOW; "
         "facts: [F_PAYMENT_SUBSIDY_STATUS, F_PAYMENT_PAYER]; "
         "rule: \"F_PAYMENT_SUBSIDY_STATUS = 'TRUE' AND F_PAYMENT_PAYER = 'STATE_GOVERNMENT'\""),
        ("CT", "CT_GOVT_PAYER", "NA", 'question: "Is the Government payment a subsidy or a price?"; '
                                      "trigger: \"F_PAYMENT_PAYER IN ['CENTRAL_GOVERNMENT','STATE_GOVERNMENT']\"; "
                                      "outcomes: [LE_CIPFS]"),
        ("TC", "TC1", "NA", "name: \"Goods paid in kind\"; facts: {F_SUPPLY_SUBJECT: 'GOODS', F_PAYMENT_FORM: 'NON_MONEY', "
                            "F_PAYMENT_SUPPLY_LINK: TRUE, F_PAYMENT_SUBSIDY_STATUS: FALSE, F_PAYMENT_PAYER: 'RECIPIENT'}; "
                            "expect: LE_CIPFS=TRUE"),
        ("TC", "TC2", "NA", "name: \"State subsidy only\"; facts: {F_SUPPLY_SUBJECT: 'GOODS', F_PAYMENT_FORM: 'MONEY', "
                            "F_PAYMENT_SUPPLY_LINK: TRUE, F_PAYMENT_SUBSIDY_STATUS: TRUE, F_PAYMENT_PAYER: 'STATE_GOVERNMENT'}; "
                            "expect: LE_CIPFS=FALSE"),
    ])
    akp += _rows("CGSTA17", b, "M_MVAF", [
        ("MKP", "M_MVAF", "NA", 'citation: "Section 2(31)(b)"; citation_key: CGSTA17_S2_C31_SCb; knowledge_type: DEFINITION'),
        ("LE", "LE_MVAF", "NA", 'outcome: ACT_FORBEARANCE_CONSIDERATION; expr: "A_SUPPLY_RELATION AND A_ACT_FORBEAR"; '
                                'nl: "The monetary value of an act or forbearance is consideration"'),
        ("A", "A_SUPPLY_RELATION", "Supply Subject Relation",
         "statement: \"The act or forbearance is in relation to the supply of goods or services or both\"; order: LOW; "
         "facts: [F_SUPPLY_SUBJECT]; rule: \"F_SUPPLY_SUBJECT IN ['GOODS','SERVICES','BOTH']\""),
        ("A", "A_ACT_FORBEAR", "Act Or Forbearance",
         "statement: \"An act or a forbearance is given in return for the supply\"; order: HIGH; "
         "facts: [F_ACTIVITY_DESCRIPTION]; interpretation: \"Is an act or forbearance given in return for the supply?\""),
    ])
    akp += _rows("CGSTA17", p1, "M_DNCU", [
        ("MKP", "M_DNCU", "NA", 'citation: "Section 2(31) proviso"; citation_key: CGSTA17_S2_C31_P1; knowledge_type: EXCEPTION'),
        ("LE", "LE_DNCU", "NA", 'outcome: DEPOSIT_NOT_CONSIDERATION; expr: "A_DEPOSIT AND NOT A_APPLIED"; '
                                'nl: "A deposit not applied is not consideration"'),
        ("A", "A_DEPOSIT", "Deposit Given", "statement: \"A deposit is given in respect of the supply\"; order: LOW; "
                                            "facts: [F_PAYMENT_IS_DEPOSIT]; rule: \"F_PAYMENT_IS_DEPOSIT = TRUE\""),
        ("A", "A_APPLIED", "Deposit Applied", "statement: \"The supplier applies the deposit as consideration\"; "
                                              "order: LOW; facts: [F_DEPOSIT_APPLIED]; rule: \"F_DEPOSIT_APPLIED = TRUE\""),
        ("LK", "LK1", "NA", "relation: OVERRIDES; target: CGSTA17_S2_C31_SCa; status: EXPLICIT"),
    ])
    akp += _rows("CGSTA17", s15, "M_VNDTV", [
        ("MKP", "M_VNDTV", "NA", 'citation: "Section 15(4)"; citation_key: CGSTA17_S15_SS4; knowledge_type: CONDITION'),
        ("LE", "LE_VNDTV", "NA", 'outcome: TRANSACTION_VALUE_NOT_USABLE; expr: "A_TV_NOT_DETERMINABLE"; '
                                 "feeds: [PD_TV_NOT_DETERMINABLE]"),
        ("A", "A_TV_NOT_DETERMINABLE", "Value Not Determinable",
         "statement: \"The value cannot be determined under sub-section (1)\"; order: LOW; facts: [F_PRICE_SOLE_CONSIDERATION]; "
         "rule: \"F_PRICE_SOLE_CONSIDERATION = FALSE\""),
        ("A", "A_DAYS", "Time Limit Check", "statement: \"The value is fixed within thirty days of supply\"; order: LOW; "
                                           "facts: [F_PRICE_SOLE_CONSIDERATION]; rule: \"DAYS_BETWEEN(F_A, F_B) <= 30\""),
        ("LE", "LE_UNUSED", "NA", 'outcome: TIME_NOTE; expr: "A_DAYS"'),
    ])
    akp += _rows("CGSTR17", r27, "M_VSCNWM", [
        ("MKP", "M_VSCNWM", "NA", 'citation: "Rule 27"; citation_key: CGSTR17_R27; knowledge_type: COMPUTATION'),
        ("LE", "LE_V27A", "NA", 'outcome: VALUE_OPEN_MARKET; expr: "PD_TRANSACTION_IS_SUPPLY AND PD_CONSIDERATION_INCLUDED '
                                'AND PD_TV_NOT_DETERMINABLE AND A_NWIM AND A_OMVA"; nl: "Rule 27(a) open market value"'),
        ("LE", "LE_V27B", "NA", 'outcome: VALUE_MONEY_PLUS; expr: "PD_TRANSACTION_IS_SUPPLY AND PD_CONSIDERATION_INCLUDED '
                                'AND PD_TV_NOT_DETERMINABLE AND A_NWIM AND NOT A_OMVA AND A_NMVK"'),
        ("OR", "OR1", "NA", "exclusive: [LE_V27A, LE_V27B]; otherwise: NONE; if_unknown: ASK"),
        ("A", "A_NWIM", "Not Wholly Money", "statement: \"The consideration is not wholly in money\"; order: LOW; "
                                            "facts: [F_PAYMENT_FORM]; rule: \"F_PAYMENT_FORM IN ['NON_MONEY','MIXED']\""),
        ("A", "A_OMVA", "Open Market Value", "statement: \"The open market value of the supply is available\"; "
                                             "order: LOW; facts: [F_OMV_AVAILABLE]; rule: \"F_OMV_AVAILABLE = TRUE\""),
        ("A", "A_NMVK", "Equivalent Value Known", "statement: \"The money value of the consideration not in money is known\"; "
                                                  "order: LOW; facts: [F_NON_MONEY_VALUE_KNOWN]; rule: \"F_NON_MONEY_VALUE_KNOWN = TRUE\""),
    ])
    akp += _rows("CGSTA17", s7, "M_SIAFOS", [
        ("MKP", "M_SIAFOS", "NA", 'citation: "Section 7(1)(a)"; citation_key: CGSTA17_S7_SS1_Ca; knowledge_type: SCOPE'),
        ("LE", "LE_SIAFOS", "NA", 'outcome: SUPPLY_INCLUDED; expr: "A_BARTER"'),
        ("A", "A_BARTER", "Barter Form", "statement: \"The supply is a barter\"; order: LOW; facts: [F_SUPPLY_FORM]; "
                                         "rule: \"F_SUPPLY_FORM = 'BARTER'\""),
    ])
    facts = {
        "F_SUPPLY_SUBJECT": {"name": "Subject of supply", "entity": "SUPPLY", "data_type": "CODE",
                             "options": ["GOODS", "SERVICES", "BOTH", "NONE"], "evidence": ["invoice"]},
        "F_PAYMENT_FORM": {"name": "Form of payment", "entity": "PAYMENT", "data_type": "CODE",
                           "options": ["MONEY", "NON_MONEY", "MIXED"], "evidence": ["invoice", "ledger"]},
        "F_PAYMENT_SUPPLY_LINK": {"name": "Payment linked to supply", "entity": "PAYMENT", "data_type": "BOOL",
                                  "evidence": ["contract"]},
        "F_PAYMENT_SUBSIDY_STATUS": {"name": "Payment is a subsidy", "entity": "PAYMENT", "data_type": "BOOL",
                                     "evidence": ["ledger"]},
        "F_PAYMENT_PAYER": {"name": "Who pays", "entity": "PAYMENT", "data_type": "CODE",
                            "options": ["RECIPIENT", "OTHER_PERSON", "CENTRAL_GOVERNMENT", "STATE_GOVERNMENT"],
                            "evidence": ["ledger"]},
        "F_ACTIVITY_DESCRIPTION": {"name": "Description of the activity", "entity": "ACTIVITY", "data_type": "TEXT",
                                   "evidence": ["description"]},
        "F_PAYMENT_IS_DEPOSIT": {"name": "Payment is a deposit", "entity": "PAYMENT", "data_type": "BOOL",
                                 "evidence": ["ledger"]},
        "F_DEPOSIT_APPLIED": {"name": "Deposit applied", "entity": "PAYMENT", "data_type": "BOOL", "evidence": ["ledger"]},
        "F_PRICE_SOLE_CONSIDERATION": {"name": "Price is the sole consideration", "entity": "SUPPLY", "data_type": "BOOL",
                                       "evidence": ["contract"]},
        "F_OMV_AVAILABLE": {"name": "Open market value available", "entity": "SUPPLY", "data_type": "BOOL",
                            "evidence": ["price list"], "nl": "Is the open market value of the supply available?"},
        "F_NON_MONEY_VALUE_KNOWN": {"name": "Money value of non-money part known", "entity": "SUPPLY",
                                    "data_type": "BOOL", "evidence": ["contract"]},
    }
    gkp = [{"source_mkp": a, "akp_element": "A_PAY_NONMONEY", "akp_type": "A", "tg_id": "TG01",
            "tsg_id": "TSG01_03", "status": "APPROVED"},
           {"source_mkp": b, "akp_element": "A_ACT_FORBEAR", "akp_type": "A", "tg_id": "TG01",
            "tsg_id": "TSG01_03", "status": "APPROVED"},
           {"source_mkp": r27, "akp_element": "A_NWIM", "akp_type": "A", "tg_id": "TG01",
            "tsg_id": "TSG01_03", "status": "APPROVED"},
           {"source_mkp": s7, "akp_element": "A_BARTER", "akp_type": "A", "tg_id": "TG01",
            "tsg_id": "TSG01_03", "status": "APPROVED"},
           {"source_mkp": s15, "akp_element": "A_TV_NOT_DETERMINABLE", "akp_type": "A", "tg_id": "TG04",
            "tsg_id": "TSG04_02", "status": "APPROVED"}]
    return Snapshot(akp=akp, facts=facts, gkp=gkp, time_facts=[],
                    questions={r27: "How is a supply not wholly for money valued?"}, definitions=[])


@pytest.fixture(scope="module")
def built():
    return build(snapshot(), "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "TSG01_03-v1", depth=3)


def test_laws_members_links_and_spine(built):
    rep = built.report
    assert set(rep["members"]) == {"CGSTA17_S2_C31_SCa#TEXT", "CGSTA17_S2_C31_SCb#TEXT", "CGSTR17_R27#TEXT"}
    added = {x["mkp"]: x["reason"] for x in rep["added"]}
    assert added["CGSTA17_S15_SS4#TEXT"] == "PRODUCT PD_TV_NOT_DETERMINABLE"     # pulled in by Rule 27
    assert added["CGSTA17_S2_C31_P1#TEXT"].startswith("OVERRIDE")                # the deposit proviso
    assert rep["covered_by_spine"] == ["CGSTA17_S7_SS1_Ca#TEXT"]                   # s.7(1)(a) not converted again
    states = {c["atom"]: c["state"] for c in rep["coverage"]}
    assert states == {"A_PAY_NONMONEY": "INCLUDED", "A_ACT_FORBEAR": "INCLUDED", "A_NWIM": "INCLUDED",
                      "A_BARTER": "IN_SPINE"}


def test_pathway_order_spine_first_then_dependencies(built):
    ids = [c["id"] for c in built.rulebook["compounds"]]
    assert ids[:2] == ["C_MOS", "C_TOS"]
    assert ids.index("C_S2_C31") < ids.index("C_VSCNWM") and ids.index("C_VNDTV") < ids.index("C_VSCNWM")
    r27 = next(c for c in built.rulebook["compounds"] if c["id"] == "C_VSCNWM")
    assert r27["question"] == "How is a supply not wholly for money valued?"
    assert r27["exclusive"] == ["LE_V27A", "LE_V27B"]
    assert "PD_TIAS AND PD_CONSIDERATION_INCLUDED AND PD_TV_NOT_DETERMINABLE" in built.rulebook["outcomes"]["LE_V27A"]["expr"]


def test_unique_ids_and_id_map(built):
    p = built.rulebook["predicates"]
    assert "A_SUPPLY_RELATION" in p and "A_SUPPLY_RELATION_MVAF" in p                  # clash between (a) and (b)
    assert built.rulebook["outcomes"]["LE_MVAF"]["expr"] == "A_SUPPLY_RELATION_MVAF AND A_ACT_FORBEAR"
    m = {x["rulebook_id"]: x for x in built.id_map}
    assert m["A_SUPPLY_RELATION_MVAF"]["akp_id"] == "CGSTA17_S2_C31_SCb#TEXT::A_SUPPLY_RELATION"


def test_rules_and_facts_converted_for_the_engine(built):
    p, f = built.rulebook["predicates"], built.rulebook["facts"]
    assert p["A_IN_RESPECT"]["rule"] == "F_PAYMENT_SUPPLY_LINK = TRUE"                  # 'TRUE' text -> TRUE
    assert p["A_PAY_NONMONEY"]["rule"] == "F_PAYMENT_FORM = 'NON_MONEY'"               # == and "..." fixed
    assert p["A_SUPPLY_RELATION"]["rule"] == "F_SOS IN ['GOODS','SERVICES','BOTH']"     # alias to spine fact
    assert p["A_SUPPLY_RELATION"]["if_false"].startswith("No supply")
    assert p["A_ACT_FORBEAR"]["order"] == "HIGH" and p["A_ACT_FORBEAR"]["grounding"] == ["F_ACTIVITY_DESCRIPTION"]
    assert "F_SUPPLY_SUBJECT" not in f and f["F_PAYMENT_SUPPLY_LINK"]["type"] == "BOOLEAN"
    assert f["F_PAYMENT_FORM"]["tier"] == "T0" and f["F_ACTIVITY_DESCRIPTION"]["tier"] == "T1"
    assert f["F_OMV_AVAILABLE"]["ask"].endswith("?")
    for pred in p.values():
        if "rule" in pred:
            assert R.check(pred["rule"], set(f)) == []


def test_unusable_rule_goes_to_the_judge_and_override_and_trigger(built):
    p, rb = built.rulebook["predicates"], built.rulebook
    assert p["A_DAYS"]["order"] == "HIGH" and "rule" not in p["A_DAYS"]
    assert any("A_DAYS" in w for w in built.report["warnings"])
    cip = next(c for c in rb["compounds"] if c["id"] == "C_S2_C31")
    assert cip["overrides"] and rb["overrides"][cip["overrides"][0]]["expr"] == "A_DEPOSIT AND NOT A_APPLIED"
    assert rb["triggers"]["CT_GOVT_PAYER"]["beside"] == "LE_CIPFS"
    assert [x for x in rb["precedents"] if x["rulebook_version"] != "TSG01_03-v1"] == []


def test_test_cases_become_engine_transactions(built):
    tcs = {t["id"]: t for t in built.tests}
    t1 = tcs["TC_CIPFS_TC1"]
    assert t1["expect"] == {"PD_CONSIDERATION_INCLUDED": "TRUE"}
    assert t1["transaction"]["facts"]["F_SOS"] == "GOODS" and t1["transaction"]["facts"]["F_PAYMENT_SUPPLY_LINK"] is True
    assert tcs["TC_CIPFS_TC2"]["expect"] == {"PD_CONSIDERATION_INCLUDED": "FALSE"}
    assert [c["id"] for c in t1["rulebook"]["compounds"]] == ["C_TEST"]          # one-step test pack, no spine
    assert "overrides" not in t1["rulebook"]


def test_law_as_on_leaves_out_provisions_not_yet_in_force():
    snap = snapshot()
    snap.time_facts = [{"target_citation_key": "CGSTA17_S2_C31_SCb", "kind": "INSERTED", "effective_from": "2019-02-01"}]
    early = build(snap, "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "v1", 3, law_as_on="2018-12-31")
    assert "CGSTA17_S2_C31_SCb#TEXT" not in early.included
    assert any("not in force" in x["reason"] for x in early.report["excluded"])


def test_yaml_round_trip(built, tmp_path):
    p = tmp_path / "rulebook.yaml"
    p.write_text(yaml.safe_dump(built.rulebook, sort_keys=False, allow_unicode=True), encoding="utf-8")
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == built.rulebook


def test_molecule_table_for_the_manager(built):
    rows = [m for m in built.report["molecules"] if m["molecule"] == "CGSTA17_S2_C31_SCa#TEXT"]
    kinds = {(m["element_type"], m["element_id"]) for m in rows}
    assert {("A", "A_PAY_NONMONEY"), ("G", "G_PAY_FORM"), ("LE", "LE_CIPFS"), ("CT", "CT_GOVT_PAYER"),
            ("F", "F_PAYMENT_FORM"), ("F", "F_SOS"), ("TSG", "TSG01_03")} <= kinds
    b = [m for m in built.report["molecules"] if m["molecule"] == "CGSTA17_S2_C31_SCb#TEXT" and m["element_id"] == "A_SUPPLY_RELATION"]
    assert b[0]["rulebook_id"] == "A_SUPPLY_RELATION_MVAF"


def test_view_regexes_match_akp_results():
    """The same patterns the SQL views use, on real AKP RESULTS text."""
    import re
    le = 'outcome: X; expr: "G_PAY_FORM AND A_SUPPLY_RELATION AND NOT G_GOVT_SUBSIDY"; feeds: [PD_CONSIDERATION_INCLUDED]'
    expr = re.search(r'expr:\s*"([^"]*)"', le).group(1)
    assert re.findall(r"\b(?:A|G|PD)_[A-Z0-9_]+", expr) == ["G_PAY_FORM", "A_SUPPLY_RELATION", "G_GOVT_SUBSIDY"]
    g = "name: \"Form\"; logic: ANY; members: [A_PAY_MONEY, A_PAY_NONMONEY]"
    assert [x.strip(" '\"") for x in re.search(r"members:\s*\[([^\]]*)\]", g).group(1).split(",")] == ["A_PAY_MONEY", "A_PAY_NONMONEY"]
