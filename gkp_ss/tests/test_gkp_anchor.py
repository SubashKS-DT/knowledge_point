"""GKP candidates without a database: the most specific taxonomy anchor wins.
Run: python -m pytest -q gkp_ss/tests/test_gkp_anchor.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gkp_ss import taxonomy as tax
from gkp_ss.candidates import CandidateFinder
data, subs = tax.load(ROOT / 'Config' / 'gst_taxonomy.json')
by_id={x["tsg_id"]:x for x in subs}
class S:
    def t(self,n): return n
    def approved_for_provision(self,p): return []
    def subgroups_by_fact(self,f,m): return []


def test_specific_anchor_beats_broad_schedule_anchor():
    f=CandidateFinder(S(),by_id,{"max_candidates":12})
    core={g["tg_id"] for g in data["groups"] if g["tg_id"]!="TG18"}
    c=f.find({"source_mkp":"CGSTA17_SCHII_EN1#TEXT","results":'statement: "x"',"concept":"","family":""},core,5)
    src={x["tsg_id"]:x["sources"] for x in c}
    assert src["TSG01_02"]==["anchor CGSTA17_SCHII_EN1"]
    assert all(s.startswith("broad") for x in ("TSG01_08","TSG03_04") for s in src[x])
    assert [x["tsg_id"] for x in c][0]=="TSG01_02"

