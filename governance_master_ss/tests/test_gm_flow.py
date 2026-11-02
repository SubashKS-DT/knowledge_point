"""GOVERNANCE MASTER governance masters without a database: which governance masters are updated, the Python self-check, the main-table rows,
the change summary and the governance master folder.
Run: python -m pytest -q governance_master_ss/tests
"""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import governance_master_engine as p4  # noqa: E402
from test_gm_build import CFG, SPINE, snapshot  # noqa: E402
from governance_master_ss import pack as pack_mod  # noqa: E402
from governance_master_ss.build import build  # noqa: E402

S = {"prerequisite_depth": 3, "law_as_on": ""}
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
NAMES = {"tsg": "TSG01_03", "tsg_name": "Barter & Exchange", "tg": "TG01", "tg_name": "GOODS"}


def _built(snap):
    return build(snap, "TSG01_03", "Barter & Exchange", "TG01", "GOODS", CFG, SPINE, "TSG01_03", 3)


def test_python_self_check_passes_and_catches_errors():
    b = _built(snapshot())
    assert pack_mod.check_pack(b.rulebook) == []
    bad = copy.deepcopy(b.rulebook)
    bad["groups"]["G_PAY_FORM"]["members"].append("A_MISSING")
    bad["outcomes"]["LE_CIPFS"]["expr"] += " AND PD_NOBODY_MAKES_THIS"
    errs = pack_mod.check_pack(bad)
    assert any("A_MISSING" in e for e in errs) and any("PD_NOBODY_MAKES_THIS" in e for e in errs)


def test_main_table_rows_are_readable_and_traceable():
    b = _built(snapshot())
    rows = pack_mod.rows(b.rulebook, b.report, b.id_map, SPINE, NAMES, "APPROVED")
    by = {(r["section"], r["element_id"]): r for r in rows}
    head = by[("HEADER", "TSG01_03")]
    assert head["tsg_name"] == "Barter & Exchange" and "CGSTR17_R27#TEXT" in head["attributes"]["laws"]
    a = by[("CHECK", "A_PAY_NONMONEY")]
    assert a["text"] == "The payment is otherwise than in money" and a["logic"] == "F_PAYMENT_FORM = 'NON_MONEY'"
    assert a["check_order"] == "LOW" and a["facts"] == "F_PAYMENT_FORM" and a["included_as"] == "MEMBER"
    assert a["source_mkp"] == "CGSTA17_S2_C31_SCa#TEXT" and a["akp_id"].endswith("::A_PAY_NONMONEY") and a["step_id"] == "C_S2_C31"
    assert by[("CHECK", "A_SIMFC")]["included_as"] == "SPINE"
    assert by[("STEP", "C_MOS")]["element_type"] == "GATE" and by[("STEP", "C_MOS")]["step_no"] == 1
    assert by[("OUTCOME", "LE_V27A")]["included_as"] == "MEMBER"
    assert by[("CHECK", "A_TV_NOT_DETERMINABLE")]["included_as"] == "PRODUCT PD_TV_NOT_DETERMINABLE"
    assert by[("FACT", "F_PAYMENT_FORM")]["check_order"] == "T0"


def test_change_summary():
    b = _built(snapshot())
    rows = pack_mod.rows(b.rulebook, b.report, b.id_map, SPINE, NAMES, "APPROVED")
    old = {(r["section"], r["element_id"]): json.dumps(r["attributes"]) for r in rows}
    assert pack_mod.change(old, rows) == {"added": [], "removed": [], "changed": []}
    old.pop(("CHECK", "A_OMVA"))
    old[("CHECK", "A_GONE")] = "{}"
    old[("CHECK", "A_NWIM")] = json.dumps({"statement": "old wording"})
    c = pack_mod.change(old, rows)
    assert c == {"added": ["CHECK:A_OMVA"], "removed": ["CHECK:A_GONE"], "changed": ["CHECK:A_NWIM"]}


def test_unrelated_law_keeps_the_fingerprint_and_a_changed_one_does_not():
    a = snapshot()
    fp = p4.fingerprint(a, _built(a), "TSG01_03", CFG, SPINE, S)
    b = copy.deepcopy(a)
    b.gkp.append({"source_mkp": "CGSTA17_S25#TEXT", "akp_element": "A_X", "akp_type": "A", "tg_id": "TG05",
                  "tsg_id": "TSG05_01", "status": "APPROVED"})
    assert p4.fingerprint(b, _built(b), "TSG01_03", CFG, SPINE, S) == fp
    c = copy.deepcopy(a)
    for r in c.akp:
        if r["source_mkp"] == "CGSTR17_R27#TEXT" and r["element"] == "A_OMVA":
            r["results"] += "; nl: \"changed\""
    assert p4.fingerprint(c, _built(c), "TSG01_03", CFG, SPINE, S) != fp


class _Store:
    def __init__(self, latest):
        self.latest = latest

    def latest_all(self):
        return self.latest


def test_tsgs_of_the_mkps_and_packs_that_use_them():
    snap = snapshot()
    store = _Store({"TSG04_02": {"laws": ["CGSTA17_S15_SS4#TEXT"], "changed_at": T0},
                    "TSG09_01": {"laws": ["CGSTA17_S10#TEXT"], "changed_at": T0}})
    rule27 = sorted({g["source_mkp"] for g in snap.gkp if g["source_mkp"].startswith("CGSTR17_R27")})
    assert p4.tsgs_for_mkps(snap, rule27, store) == ["TSG01_03"]                # Rule 27 is grouped in Barter & Exchange
    assert "TSG04_02" in p4.tsgs_for_mkps(snap, ["CGSTA17_S15_SS4#TEXT"], store)  # used there
    assert p4.tsgs_for_mkps(snap, [], store) == []


def test_governance_master_folder_written_once_and_left_alone_when_unchanged(tmp_path):
    perm = __import__("kp_settings").load()
    s = p4.validate_governance_master_settings({**perm["governance_master"], "save_to_db": False, "use_cache": True,
                                                "output_dir": str(tmp_path / "Governance_Master")})
    r = p4.build_governance_master("TSG01_03", snapshot(), None, perm, s)
    f = tmp_path / "Governance_Master" / "TG01" / "TSG01_03" / "rulebook.yaml"
    assert r["status"] == "WRITTEN" and f.is_file(), r
    assert (f.parent / "report.xlsx").is_file() and not (f.parent / "versions").exists()
    first = f.stat().st_mtime_ns
    r2 = p4.build_governance_master("TSG01_03", snapshot(), None, perm, s)
    assert r2["status"] == "WRITTEN" and f.stat().st_mtime_ns == first      # same content: file not rewritten


def test_spine_only_tsg_still_gets_a_governance_master(tmp_path):
    perm = __import__("kp_settings").load()
    s = p4.validate_governance_master_settings({**perm["governance_master"], "save_to_db": False,
                                                "output_dir": str(tmp_path / "Governance_Master")})
    snap = snapshot()
    snap.gkp = [g for g in snap.gkp if g["source_mkp"].startswith("CGSTA17_S7")]
    r = p4.build_governance_master("TSG01_03", snap, None, perm, s)
    f = tmp_path / "Governance_Master" / "TG01" / "TSG01_03" / "rulebook.yaml"
    assert r["status"] == "WRITTEN" and f.is_file()                        # the spine itself is the governance master
    assert "A_AIAB:" in f.read_text(encoding="utf-8")
