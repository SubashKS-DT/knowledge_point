"""MKP cache per provision and 'new version only when different' - no database, no model.

Needs the IKP of the CGST Act as JSONL (Output/ikp/CGSTA17.ikp.jsonl, written by IKP); skipped when it is not there.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import mkp_engine as mkp  # noqa: E402
from ikp_ss.reader import IkpIndex  # noqa: E402
from mkp_ss.draft import Drafter  # noqa: E402
from mkp_ss.scope import batches_for, resolve_token  # noqa: E402
from mkp_ss.store import _signatures  # noqa: E402

JSONL = ROOT / "Output" / "ikp" / "CGSTA17.ikp.jsonl"


class FakeStore:
    def __init__(self, saved):
        self.saved = saved                       # element -> {"text", "knowledge_type"}

    def stored_mkps(self, version, keys):
        return {k: dict(v, parent="", version=1, phrases=[]) for k, v in self.saved.items() if k in keys}

    def registry_slice(self, *a, **k):
        return []


@pytest.fixture(scope="module")
def run():
    if not JSONL.is_file():
        pytest.skip("IKP JSONL of the CGST Act not available")
    idx = IkpIndex.from_jsonl(JSONL)
    r = object.__new__(mkp.MkpRun)
    r.idx, r.version, r.code = idx, "V_TEST", "CGSTA17"
    r.doc = {"code": "CGSTA17", "name": "CGST Act", "type": "ACT"}
    r.drafter = Drafter(idx, "ACT", "CGST Act")
    r.s = {"use_cache": True, "verify_blocks": False, "complex_threshold": 20, "judge_retries": 0,
           "fidelity_check": "BLOCK", "registry_limit": 5,
           "models": {t: {k: {"llm": "CLAUDE", "model": "x"} for k in mkp.ROLES} for t in ("SIMPLE", "COMPLEX")}}
    return r


def _section7(run):
    return [b for b in batches_for(run.idx, resolve_token(run.idx, "section 7"))][0]


def test_all_saved_means_no_model_call(run):
    from mkp_ss.source import unit_context
    batch = _section7(run)
    drafts = unit_context(run.drafter, batch, run.doc)["drafts"]
    run.store = FakeStore({d.mkp_key: {"text": d.text, "knowledge_type": "HEADING" if d.kind == "HEADING" else "SCOPE"}
                           for d in drafts})
    run.models = None                            # any model call would fail
    assert run.run_unit(batch)["status"] == "CACHED"


def test_only_missing_mkps_go_to_the_models(run):
    from mkp_ss.source import unit_context
    batch = _section7(run)
    drafts = unit_context(run.drafter, batch, run.doc)["drafts"]
    saved_7_1 = {d.mkp_key: {"text": d.text, "knowledge_type": "SCOPE"} for d in drafts
                 if d.citation_key.startswith("CGSTA17_S7_SS1_") and d.kind != "HEADING"}
    run.store = FakeStore(saved_7_1)
    state = run.cache_state(drafts)
    assert state["done"] and state["missing"] and not state["changed"]
    only = {d.mkp_key for d in state["missing"]}
    ctx = unit_context(run.drafter, batch, run.doc, only_keys=only)
    sent = {d.mkp_key for d in ctx["drafts"]}
    assert sent == only and not sent & set(saved_7_1)        # 7(1) is reused, never sent again


def test_changed_source_text_is_reported(run):
    from mkp_ss.source import unit_context
    batch = _section7(run)
    drafts = unit_context(run.drafter, batch, run.doc)["drafts"]
    d = next(x for x in drafts if x.kind == "TEXT")
    run.store = FakeStore({d.mkp_key: {"text": d.text.replace("supply", "sale", 1), "knowledge_type": "SCOPE"}})
    state = run.cache_state(drafts)
    assert [x.mkp_key for x, _ in state["changed"]] == [d.mkp_key]


def test_same_answer_keeps_the_version():
    old = [{"TYPE": "MKP", "ELEMENT": "K#TEXT", "RESULTS": 'concept: "A"; knowledge_type: SCOPE; text: "The supply, of goods."'},
           {"TYPE": "SP", "ELEMENT": "K#TEXT", "RESULTS": 'sp: SP1; text: "The supply, of goods."'}]
    new = [{"TYPE": "MKP", "ELEMENT": "K#TEXT", "RESULTS": 'concept: "Other words"; knowledge_type: SCOPE; text: "The supply of goods"'},
           {"TYPE": "SP", "ELEMENT": "K#TEXT", "RESULTS": 'sp: SP1; text: "The supply of goods"'}]
    assert _signatures(old)["K#TEXT"]["key"] == _signatures(new)["K#TEXT"]["key"]      # concept wording is not a change
    new[0]["RESULTS"] = new[0]["RESULTS"].replace("supply", "sale")
    assert _signatures(old)["K#TEXT"]["key"] != _signatures(new)["K#TEXT"]["key"]
