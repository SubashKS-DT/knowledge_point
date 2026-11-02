"""IKP on notifications: the operative text starts at the enacting formula ("In exercise of the powers ...")
when there is no "Subject:" line and no "1." paragraph; clauses, wrap-up text and later paragraphs follow.
Needs PyMuPDF and the notification PDFs in Law/ (skips otherwise).
Run: python -m pytest -q ikp_ss/tests/test_ikp_notifications.py
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
pytest.importorskip("pymupdf")
from ikp_ss.build import analyse, document_quality  # noqa: E402


def _pdf(number: str) -> Path:
    hits = sorted((ROOT / "Law").glob(f"**/{number}-Central*Tax*Rate*.pdf"))
    if not hits:
        pytest.skip(f"Law/{number}-Central Tax_Rate.pdf not present")
    return hits[0]


def _run(number: str, code: str):
    a = analyse(_pdf(number), code, "NOTIFICATION")
    return a, document_quality(a), a.parsed.nodes


def test_single_paragraph_notification_with_clauses_and_wrap_up():
    a, q, n = _run("04_2018", "N04_2018")
    assert q.status != "FAIL"
    assert {"N04_2018_PA1", "N04_2018_PA1_Ca", "N04_2018_PA1_Cb", "N04_2018_PA1_CL1", "N04_2018_SIGNATURE"} <= set(n)
    assert a.law_as_on_iso == "2018-01-25"


def test_amending_notification_gets_its_own_date_not_the_principal_one():
    a, q, n = _run("23_2019", "N23_2019")
    assert q.status != "FAIL" and {"N23_2019_PA1", "N23_2019_PA2"} <= set(n)
    assert a.law_as_on_iso == "2019-09-30"                 # not 25 January 2018 (the notification it amends)


def test_two_lists_explanation_paragraph_and_commencement():
    a, q, n = _run("06_2019", "N06_2019")
    assert q.status != "FAIL"
    assert {"N06_2019_PA1_SCi", "N06_2019_PA1_SCii", "N06_2019_PA1_CL1", "N06_2019_PA2", "N06_2019_PA3"} <= set(n)
    assert {"N06_2019_PA1_CL1_Ca", "N06_2019_PA1_CL1_Cd", "N06_2019_PA1_CL1_CL1"} <= set(n)   # (a)-(d) + "shall arise ..."
    assert a.law_as_on_iso == "2019-03-29"
