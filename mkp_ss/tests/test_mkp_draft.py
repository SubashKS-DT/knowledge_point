"""MKP drafter regression: Python-written MKP text must match the worked examples and keep every word.
Run: python -m pytest -q mkp_ss/tests
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import ikp_engine as p0  # noqa: E402
from ikp_ss.reader import IkpIndex  # noqa: E402
from mkp_ss.draft import Drafter  # noqa: E402
from mkp_ss.check import fidelity  # noqa: E402
from mkp_ss.scope import batches_for  # noqa: E402


@pytest.fixture(scope="module")
def act():
    try:
        conn = p0.get_connection({"env_file": ROOT / "Credentials" / ".env"})
        idx = IkpIndex.from_db(conn, "CGSTA17")
        conn.close()
    except Exception as exc:                      # no database: use the IKP JSONL export
        jsonl = ROOT / "Output" / "ikp" / "CGSTA17.ikp.jsonl"
        if not jsonl.is_file():
            pytest.skip(f"database not reachable and no JSONL: {exc}")
        idx = IkpIndex.from_jsonl(jsonl)
    return Drafter(idx, "ACT", "CGST Act")


def _texts(d, root):
    return {x.mkp_key: x.text for x in d.draft(root)}


def test_section_7_worked_example(act):
    t = _texts(act, "CGSTA17_S7")
    assert t["CGSTA17_S7#HEAD"] == "Section 7 deals with scope of supply."
    assert t["CGSTA17_S7_SS1_Cb#TEXT"] == ('For the purposes of this Act, the expression "supply" includes import of services '
                                          'for a consideration whether or not in the course or furtherance of business.')
    assert t["CGSTA17_S7_SS2_Ca#TEXT"].startswith("Notwithstanding anything contained in sub-section (1), activities")
    assert "CGSTA17_S7_SS1_Cd#TEXT" not in t                  # omitted clause -> amendment MKP only


def test_schedule_entry_heading_is_not_dropped(act):
    t = _texts(act, "CGSTA17_SCHIII_EN6")
    assert t == {"CGSTA17_SCHIII_EN6#TEXT": "Actionable claims, other than lottery, betting and gambling."}
    t5 = _texts(act, "CGSTA17_SCHIII_EN5")["CGSTA17_SCHIII_EN5#TEXT"]
    assert t5.startswith("Sale of land and, subject to clause (b) of paragraph 5 of Schedule II, sale of building")
    t3 = _texts(act, "CGSTA17_SCHII_EN3")
    assert any(v.startswith("Any treatment or process") for v in t3.values())   # real short heading removed


def test_every_batch_of_the_act_drafts_something(act):
    empty = [b.key for b in batches_for(act.idx, "CGSTA17") if not act.draft(b.root, b.with_unit_heading,
                                                                             b.with_container_heading)]
    assert not empty, empty


def test_fidelity_ignores_spacing_but_not_words():
    ref = 'For the purposes of this Act, the expression "supply" includes import of services.'
    assert fidelity('For the purposes of this Act, the expression "supply" includes import of ser- vices.', ref) == ""
    assert "drops 'the'" in fidelity('For purposes of this Act, the expression "supply" includes import of services.', ref)
    assert "'services' -> 'goods'" in fidelity('For the purposes of this Act, the expression "supply" includes import of goods.', ref)
