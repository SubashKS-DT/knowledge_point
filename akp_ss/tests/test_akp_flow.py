"""AKP flow tests: fake Gemini / Claude / judge / comparator in the real formats, local PostgreSQL.
MKP rows are produced first by a MKP run (fake models) into a test table.
Run: python -m pytest -q akp_ss/tests
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mkp_ss" / "tests"))
import ikp_engine as p0  # noqa: E402
import mkp_engine as p1  # noqa: E402
import akp_engine as p2  # noqa: E402
from transport import get_field  # noqa: E402

MKP_T, AKP_T = "mkp_p2test", "pass_2t_akp"
OVR = {"enabled": True, "mkp_table": MKP_T, "table": AKP_T, "cache_table": AKP_T + "_cache", "ask_user_input": False,
       "excel_file": "akp_test.xlsx", "use_cache": True}


def _q(t):
    return '"' + str(t).replace(";", ",").replace("|", "/").replace('"', '\\"') + '"'


def _json_after(prompt, marker="\nINPUT:\n"):
    part = prompt.rsplit(marker, 1)[1]
    obj, _ = json.JSONDecoder().raw_decode(part)
    return obj


def akp_table(src: dict, flip=False) -> str:
    """A valid conceptual AKP table for one MKP: one atom (whole proposition) testing a shared registry fact."""
    mkp_id = src["mkp_id"]
    text = get_field(src["mkp"], "text")
    statement = text.replace("without a consideration", "for a consideration") if flip else text
    rule = "F_SUPPLY_CONSIDERATION = 'PAID'" if flip else "F_SUPPLY_CONSIDERATION = 'NONE'"
    reused = any(f["fact_id"] == "F_SUPPLY_CONSIDERATION" for f in src.get("fact_registry", []))
    rows = [
        f"{mkp_id}|MKP|{mkp_id}|NA|citation_key: {get_field(src['mkp'], 'citation_key')}; frame: OPERATIVE",
        f'{mkp_id}|LE|LE_MAIN|NA|outcome: PROVISION_APPLIES; expr: "A_MAIN"',
        f'{mkp_id}|A|A_MAIN|Provision Condition Test|family: "Provision Test"; statement: {_q(statement)}; phrases: [SP1]; '
        f'scope: TRANSACTION; role: CONDITION; order: LOW; resolver: EXPRESSION; facts: [F_SUPPLY_CONSIDERATION]; rule: "{rule}"; '
        f'nl: "test"',
        f'TXN|FACTS|F_SUPPLY_CONSIDERATION|NA|name: "Consideration for the supply"; entity: SUPPLY; type: CODE; '
        f'options: [PAID, AGREED, NONE]; evidence: [invoice, contract]; registry: {"EXISTING" if reused else "NEW"}; used_by: [A_MAIN]',
    ]
    for sp in src["sp_rows"]:
        rows.append(f"{mkp_id}|SP|{get_field(sp, 'sp')}|NA|role: OPERATIVE; text: {_q(get_field(sp, 'text'))}; elements: [A_MAIN]")
    rows.append(f"{mkp_id}|SUM|{mkp_id}|NA|atoms: 1; facts: 1")
    return "MKP|TYPE|ELEMENT|CONCEPT|RESULTS\n" + "\n".join(rows)


class Fake:
    def __init__(self, judge="GOOD", compare="TRUE", same="NONE"):
        self.judge, self.compare, self.same = judge, compare, same
        self.calls = {"GEMINI": 0, "CLAUDE": 0, "JUDGE": 0, "COMPARE": 0}

    def __call__(self, provider, model, prompt):
        if "AKP SAME-MEANING ATOM CHECK" in prompt or '"new_atoms": ' in prompt.rsplit("\nINPUT:\n", 1)[-1][:200000]:
            self.calls["SAME"] = self.calls.get("SAME", 0) + 1
            data = _json_after(prompt)
            if self.same != "FIRST":
                return json.dumps({"same_matches": []})
            item = data["same_candidates"][0]
            return json.dumps({"same_matches": [{"element": item["element"], "canonical": item["candidates"][0]["akp_id"],
                                                 "same_verdict": "TRUE", "reason": "same proposition and fact"}]})
        if "MKP/AKP RECONSTRUCTION COMPARATOR" in prompt:
            self.calls["COMPARE"] += 1
            false = self.compare == "ALWAYS_FALSE" or (self.compare == "FALSE_ONCE" and self.calls["COMPARE"] == 1)
            return json.dumps({"verdict": "FALSE" if false else "TRUE", "reconstructed_mkp": "x",
                               "missing": ["SP2 missing"] if false else [], "added_or_changed": [], "logic_problems": [],
                               "same_matches": [], "references": []})
        if "AKP STANDARD AKP JUDGE" in prompt:
            self.calls["JUDGE"] += 1
            data = _json_after(prompt)
            src = data["source"]
            if "previous_comparison_false" in data and self.judge != "ALWAYS_FLIP":   # a real judge fixes what was listed
                src = {**src, "sp_rows": src["sp_rows"]}
                return akp_table(src).replace("atoms: 1; facts: 1", "atoms: 1; facts: 1; fixed: SP2")
            flip = "without a consideration" in get_field(src["mkp"], "text") and (
                self.judge == "ALWAYS_FLIP" or (self.judge == "FLIP_ONCE" and "PYTHON_CHECK_FAILED - fix exactly" not in prompt))
            out = akp_table(src, flip)
            return out.replace("nl: \"test\"", "nl: \"test, reworded\"") if self.judge == "NEW_WORDING" else out
        self.calls[provider] += 1
        return akp_table(_json_after(prompt), flip=provider == "GEMINI")


def fake_embed(text):
    v = [0.0] * 8
    for w in str(text).lower().split():
        v[hash(w) % 8] += 1.0
    return v if any(v) else None


@pytest.fixture(scope="module")
def env():
    permission = __import__("kp_settings").load()
    paths = p1.build_paths(permission)
    try:
        conn = p0.get_connection(paths)
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")
    with conn.cursor() as cur:
        for t in (MKP_T, MKP_T + "_cache", MKP_T + "_llm_cache", AKP_T, AKP_T + "_cache", AKP_T + "_llm_cache",
                  "pass_2t_fact", "pass_2t_atom_fact", "pass_2t_time_fact"):
            cur.execute(f"DROP TABLE IF EXISTS public.{t}")
    conn.commit()
    from test_mkp_flow import Fake as P1Fake, fake_embed as p1_embed
    s1 = p1.validate_mkp_settings({**permission["mkp"], "enabled": True, "ask_user_input": False, "table": MKP_T, "export_excel": False})
    doc = next(dict(v, document_key=k) for k, v in permission["documents"].items() if v["code"].upper() == "CGSTA17")
    run = p1.MkpRun(doc, permission, paths, s1, conn, embed=p1_embed)
    p1.run_mkp_document(doc, permission, paths, s1, conn, scope_roots=["CGSTA17_S7", "CGSTA17_S9"],
                          fake_llm=P1Fake(run.drafter), embed=p1_embed)
    (paths["output_dir"] / "akp_failed_mkps.txt").write_text("", encoding="utf-8")
    yield conn, paths
    conn.close()


def go(scope, fake, embed=None, **over):
    return p2.run_akp_scope(ROOT / "Law" / "Acts" / "CGST-Act.pdf", "CGSTA17", scope_key=scope, fake_llm=fake,
                              embed=embed or fake_embed, settings_override={**OVR, **over})


def q(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        out = cur.fetchall()
    conn.commit()                      # never leave a read transaction open (it would block ALTER TABLE)
    return out


def test_section_7_flow_facts_time_facts_versions(env):
    conn, paths = env
    fake = Fake(judge="FLIP_ONCE")
    s = go("CGSTA17_S7", fake)
    c = s.counts
    assert c["failed"] == 0 and c["approved"] >= 8 and c["python_written"] >= 4
    # judge flipped 'without' once on 7(1)(c): Python caught it, the judge repaired it, the saved atom keeps 'without'
    st = q(conn, f"SELECT results FROM {AKP_T} WHERE source_mkp='CGSTA17_S7_SS1_Cc#TEXT' AND type='A' AND is_latest")
    assert "without a consideration" in get_field({"RESULTS": st[0][0]}, "statement")
    assert fake.calls["JUDGE"] == c["approved"] + 1
    # one canonical fact shared by every operative atom
    facts = q(conn, "SELECT fact_id FROM pass_2t_fact")
    edges = q(conn, "SELECT count(*) AS n, count(DISTINCT source_mkp) AS m FROM pass_2t_atom_fact WHERE is_latest")[0]
    assert facts == [("F_SUPPLY_CONSIDERATION",)] and edges[0] == c["approved"] and edges[1] == c["approved"]
    # amendments became time facts on the provision they amend; headings written without LLM
    tf = q(conn, "SELECT target_citation_key, kind, effective_from FROM pass_2t_time_fact WHERE target_citation_key='CGSTA17_S7_SS1A'")
    assert tf and tf[0][1] == "INSERTED" and str(tf[0][2]) == "2017-07-01"          # 'w.e.f. 01st July, 2017'
    by = q(conn, "SELECT amended_by FROM pass_2t_time_fact WHERE target_citation_key='CGSTA17_S7_SS1A'")[0][0]
    assert "(Amendment) Act, 2018" in by
    rows = q(conn, f"SELECT akp_version, is_latest, validation_status, inserted_at FROM {AKP_T}")
    assert all(r[0] == 1 and r[1] and r[2] == "APPROVED" and r[3] for r in rows)


def test_rerun_with_cache_is_skipped(env):
    fake = Fake()
    s = go("CGSTA17_S7", fake)
    assert s.counts["cached"] == s.counts["mkps"] and sum(fake.calls.values()) == 0


def test_rerun_without_cache_same_answer_keeps_version(env):
    """use_cache=false asks the LLM again; the same atoms are not saved again."""
    conn, _ = env
    go("CGSTA17_S7", Fake(), use_cache=False, only_mkp="CGSTA17_S7_SS1_Cb#TEXT")
    got = q(conn, f"SELECT akp_version, is_latest, validation_status FROM {AKP_T} "
                  "WHERE source_mkp='CGSTA17_S7_SS1_Cb#TEXT' GROUP BY 1, 2, 3 ORDER BY 1")
    assert got == [(1, True, "APPROVED")]


def test_rerun_with_a_different_answer_saves_next_version(env):
    conn, _ = env
    go("CGSTA17_S7", Fake(judge="NEW_WORDING"), use_cache=False, only_mkp="CGSTA17_S7_SS1_Cb#TEXT")
    got = q(conn, f"SELECT akp_version, is_latest, validation_status, count(*) FROM {AKP_T} "
                  "WHERE source_mkp='CGSTA17_S7_SS1_Cb#TEXT' GROUP BY 1, 2, 3 ORDER BY 1")
    assert [g[:3] for g in got] == [(1, False, "SUPERSEDED"), (2, True, "APPROVED")]
    # GKP reads validation_status='APPROVED' - it sees only version 2
    assert q(conn, f"SELECT DISTINCT akp_version FROM {AKP_T} WHERE source_mkp='CGSTA17_S7_SS1_Cb#TEXT' "
                   "AND validation_status='APPROVED'") == [(2,)]
    assert q(conn, "SELECT count(*) FROM pass_2t_atom_fact WHERE source_mkp='CGSTA17_S7_SS1_Cb#TEXT' AND is_latest")[0][0] == 1


def test_flip_that_is_never_fixed_is_not_saved(env):
    conn, paths = env
    fake = Fake(judge="ALWAYS_FLIP")
    s = go("CGSTA17_S7", fake, use_cache=False, only_mkp="CGSTA17_S7_SS1_Cc#TEXT")
    assert s.failures == ["CGSTA17_S7_SS1_Cc#TEXT"] and fake.calls["COMPARE"] == 0
    latest = q(conn, f"SELECT results FROM {AKP_T} WHERE source_mkp='CGSTA17_S7_SS1_Cc#TEXT' AND type='A' AND is_latest")
    assert "without a consideration" in latest[0][0]                        # version 1 untouched
    assert "NEGATION FLIPPED" in (paths["output_dir"] / "akp_failed_mkps.txt").read_text(encoding="utf-8")


def test_comparison_false_then_true_on_second_attempt(env):
    fake = Fake(compare="FALSE_ONCE")
    s = go("CGSTA17_S9", fake)
    assert s.counts["failed"] == 0 and fake.calls["COMPARE"] >= 2


def test_excel_and_gkp_version_lookup(env):
    conn, paths = env
    import openpyxl
    wb = openpyxl.load_workbook(paths["output_dir"] / "akp_test.xlsx", read_only=True)
    assert {"Gemini CGST Act", "Claude CGST Act", "Claude STD CGST Act", "Facts", "Time facts"} <= set(wb.sheetnames)
    version = p2._version(conn, "CGSTA17", MKP_T, "CGSTA17_S7")
    assert version.startswith("V_")


def test_guard_unit_cases():
    from akp_ss.guard import legal_words
    src = "Every registered person shall furnish the return not later than the twentieth day and pay 18 per cent."

    def A(st):
        return {"MKP": "M", "TYPE": "A", "ELEMENT": "A_1", "CONCEPT": "X Y", "RESULTS": f'family: "F"; statement: "{st}"'}
    le = {"MKP": "M", "TYPE": "LE", "ELEMENT": "LE_1", "CONCEPT": "NA", "RESULTS": 'expr: "A_1"'}
    ok = legal_words(src, [A("A registered person shall furnish the return not later than the twentieth day and pay 18 per cent"), le])
    bad = legal_words(src, [A("A registered person may furnish the return by the twentieth day and pay 12 per cent"), le])
    assert ok == []
    assert any("MODAL" in e for e in bad) and any("NUMBER LOST" in e for e in bad) and any("NUMBER ADDED" in e for e in bad)


def test_guard_whole_words_only():
    from akp_ss.guard import legal_words
    src = "The Commissioner may notify a class of taxable persons to maintain additional accounts."
    a = {"MKP": "M", "TYPE": "A", "ELEMENT": "A_1", "CONCEPT": "X Y", "RESULTS": f'family: "F"; statement: "{src}"'}
    le = {"MKP": "M", "TYPE": "LE", "ELEMENT": "LE_1", "CONCEPT": "NA", "RESULTS": 'expr: "A_1"'}
    assert legal_words(src, [a, le]) == []                     # 'may notify' is not 'may not'


def test_guard_may_not_with_shall():
    from akp_ss.guard import legal_words
    src = ("Provided also that a registered person may not issue a tax invoice subject to the following conditions, "
           "namely, the recipient does not require such invoice, and shall issue a consolidated tax invoice.")
    a = {"MKP": "M", "TYPE": "A", "ELEMENT": "A_1", "CONCEPT": "X Y", "RESULTS": f'family: "F"; statement: "{src}"'}
    le = {"MKP": "M", "TYPE": "LE", "ELEMENT": "LE_1", "CONCEPT": "NA", "RESULTS": 'expr: "A_1"'}
    assert legal_words(src, [a, le]) == []


@pytest.mark.parametrize("note,kind,by,dated,wef", [
    ("Inserted vide Notf no. 35/2021-CT dt. 24.09.2021 w.e.f.  01.01.2022 as notified by Notf no. 38/2021", "INSERTED",
     "Notf no. 35/2021-CT", "2021-09-24", "2022-01-01"),
    ("Inserted by The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018) – Brought into force w.e.f. "
     "01st February, 2019.", "INSERTED", "The Central Goods and Services Tax (Amendment) Act, 2018 (No. 31 of 2018)", None, "2019-02-01"),
    ("Subsitituted vide Notf no.94/2020 – CT dt.22.12.2020 for ―three‖.", "SUBSTITUTED", "Notf no.94/2020 – CT", "2020-12-22", None),
    ("videNotf no. 32/2021 – CT dt. 29.08.2021 with effect from the 1st day of November, 2021, all the provisos shall be omitted.",
     "OMITTED", "Notf no. 32/2021 – CT", "2021-08-29", "2021-11-01"),
    ("Substituted for \"31st December, 2019\" by Order No. 10/2019 dated 26.12.2019.", "SUBSTITUTED", "Order No. 10/2019",
     "2019-12-26", None),
])
def test_time_fact_footnote_styles(note, kind, by, dated, wef):
    from akp_ss.facts import time_fact
    row = {"RESULTS": 'text: "Footnote 3 states: %s"; citation_key: K' % note.replace('"', '\\"')}
    tf = time_fact(row, "D", "K#AMD1")
    assert (tf["kind"], tf["amended_by"], tf["instrument_date"], tf["effective_from"]) == (kind, by, dated, wef)





def test_references_by_python_with_keys(env):
    conn, _ = env
    r = q(conn, f"SELECT results FROM {AKP_T} WHERE source_mkp='CGSTA17_S7_SS1_Cc#TEXT' AND type='A' AND is_latest")[0][0]
    assert "reference: [Schedule I]" in r and "reference_keys: [CGSTA17_SCHI]" in r
    r2 = q(conn, f"SELECT results FROM {AKP_T} WHERE source_mkp='CGSTA17_S7_SS2_Ca#TEXT' AND type='A' AND is_latest")[0][0]
    assert "Schedule III" in r2 and "CGSTA17_SCHIII" in r2 and "CGSTA17_S7_SS1" in r2     # 'sub-section (1)' too


def test_same_meaning_atom_stores_existing_akp_id(env):
    conn, _ = env
    const = lambda text: [1.0, 0, 0, 0, 0, 0, 0, 0] if text else None       # every atom 'identical' by embedding
    go("CGSTA17_S9", Fake(), embed=const, use_cache=False)                   # S9 atoms saved with this embedding
    q(conn, f"DELETE FROM {AKP_T}_llm_cache RETURNING 1")                   # raw answers are reused even with use_cache=false
    fake = Fake(same="FIRST")
    go("CGSTA17_S9", fake, embed=const, use_cache=False, only_mkp="CGSTA17_S9_SS2#TEXT")
    assert fake.calls.get("SAME") == 1
    row = q(conn, f"SELECT type, results, akp_id, same_akp_id FROM {AKP_T} WHERE source_mkp='CGSTA17_S9_SS2#TEXT' "
                  "AND element='A_MAIN' AND is_latest")[0]
    assert row[0] == "A_REF" and row[1] == "" and row[2] == "CGSTA17_S9_SS2#TEXT::A_MAIN"
    assert row[3].endswith("::A_MAIN") and not row[3].startswith("CGSTA17_S9_SS2#TEXT")   # points to another MKP's atom
    exists = q(conn, f"SELECT count(*) FROM {AKP_T} WHERE akp_id=%s AND type='A' AND is_latest", row[3])[0][0]
    assert exists == 1
    # the fact link stays, so the network still reaches this provision
    assert q(conn, "SELECT count(*) FROM pass_2t_atom_fact WHERE source_mkp='CGSTA17_S9_SS2#TEXT' AND is_latest")[0][0] == 1


def test_near_identical_but_not_same_is_kept_as_new(env):
    """7(3)(a) vs 7(3)(b): same words except goods / services -> similarity > 0.90, the SAME check runs,
    says NOT SAME, and the atom is inserted as a new atom."""
    conn, _ = env
    q(conn, f"DELETE FROM {AKP_T}_llm_cache RETURNING 1")
    fake = Fake(same="NONE")
    go("CGSTA17_S7", fake, use_cache=False, only_mkp="CGSTA17_S7_SS3_Ca#TEXT")
    assert fake.calls.get("SAME") == 1
    row = q(conn, f"SELECT type, same_akp_id, results <> '' FROM {AKP_T} WHERE source_mkp='CGSTA17_S7_SS3_Ca#TEXT' "
                  "AND element='A_MAIN' AND is_latest")[0]
    assert row == ("A", None, True)


def test_invented_reference_is_sent_back(env):
    from akp_ss.refs import RefFinder
    conn, _ = env
    f = RefFinder(conn, "CGSTA17")
    src = 'the expression "supply" includes the activities specified in Schedule I, made without a consideration'
    assert f.for_atom("The activity is listed as per schedule 1", "CGSTA17_S7_SS1_Cc", src)[:2] == (["Schedule I"], ["CGSTA17_SCHI"])
    assert f.for_atom("The activity is specified in Schedule III", "CGSTA17_S7_SS1_Cc", src)[2] == ["Schedule III"]
