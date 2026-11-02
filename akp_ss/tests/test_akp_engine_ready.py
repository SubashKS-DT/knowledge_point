"""Atoms must be usable by the engine as written (less hallucination); no database needed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from akp_ss.guard import engine_ready  # noqa: E402

FACT = {"TYPE": "FACTS", "ELEMENT": "F_SUPPLY_FORM", "RESULTS": 'name: "Form"; type: CODE; options: [BARTER, SALE]'}


def atom(el, results):
    return {"TYPE": "A", "ELEMENT": el, "RESULTS": results}


def test_good_atoms_pass():
    rows = [FACT, atom("A_BARTER", "statement: \"x\"; order: LOW; facts: [F_SUPPLY_FORM]; rule: \"F_SUPPLY_FORM = 'BARTER'\""),
            atom("A_OTHER", "statement: \"x\"; order: HIGH; facts: [F_SUPPLY_FORM]; interpretation: \"Is it another form?\"")]
    assert engine_ready(rows) == []


def test_step_number_as_order_is_refused():
    assert engine_ready([FACT, atom("A_X", 'statement: "x"; order: 1')])[0].startswith("ORDER: A_X")


def test_value_not_in_options_and_bad_grammar_are_refused():
    errs = engine_ready([FACT, atom("A_B", "statement: \"x\"; order: LOW; facts: [F_SUPPLY_FORM]; rule: \"F_SUPPLY_FORM = 'SWAP'\""),
                         atom("A_C", "statement: \"x\"; order: LOW; facts: [F_SUPPLY_FORM]; rule: \"F_SUPPLY_FORM IN (SALE)\"")])
    assert any(e.startswith("VALUE: A_B") for e in errs) and any(e.startswith("RULE: A_C") for e in errs)


def test_high_atom_needs_question_and_fact():
    errs = engine_ready([atom("A_H", 'statement: "x"; order: HIGH')])
    assert any(e.startswith("QUESTION: A_H") for e in errs) and any(e.startswith("FACTS: A_H") for e in errs)
