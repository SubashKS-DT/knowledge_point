"""GKP flow tests: MKP -> AKP (fake models) -> GKP with fake verifiers / judge / proposer, local PostgreSQL.
Run: python -m pytest -q gkp_ss/tests
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "mkp_ss" / "tests", ROOT / "akp_ss" / "tests"):
    sys.path.insert(0, str(p))
import ikp_engine as p0  # noqa: E402
import mkp_engine as p1  # noqa: E402
import akp_engine as p2  # noqa: E402
import gkp_engine as p3  # noqa: E402

MKP_T, AKP_T, PFX = "mkp_p3t", "pass_2p3_akp", "pass_3t"
TAX_TMP = ROOT / "Output" / "taxonomy_p3test.json"


def _input(prompt):
    return json.loads(prompt.rsplit("INPUT JSON:\n", 1)[1])


class Fake:
    """Verifier 1 (Gemini): TRUE when the candidate came from an anchor or an include term.
    Verifier 2 (Claude): TRUE only for anchor candidates -> term-only candidates go to the judge, who says TRUE.
    mode NOTHING: every verdict FALSE (atoms end UNCLASSIFIED -> TG18 proposal), except approved TG18 subgroups."""

    def __init__(self, mode="NORMAL"):
        self.mode, self.calls = mode, {"V1": 0, "V2": 0, "JUDGE": 0, "PROPOSE": 0}

    def __call__(self, provider, model, prompt):
        data = _input(prompt)
        if data["mode"] == "TG18_PROPOSE":
            self.calls["PROPOSE"] += 1
            return json.dumps({"proposals": [{"element": a["element"], "action": "PROPOSE", "name": "Test Gap Subject",
                                              "definition": "A narrowly stated legal concept used only by the test suite here.",
                                              "include": ["test gap"], "exclude": ["anything else"],
                                              "evidence": a["concept"], "reason": "distinct gap"} for a in data["atoms"]]})
        judge = data["mode"] == "JUDGE"
        self.calls["JUDGE" if judge else ("V1" if provider == "GEMINI" else "V2")] += 1
        out = []
        for a in data["atoms"]:
            asg = []
            for c in a["candidates"]:
                src = " ".join(c.get("sources") or [])
                if self.mode == "NOTHING":
                    ok = c["tsg_id"].startswith("TSG18_") and c["subgroup"] == "Test Gap Subject"
                elif judge:
                    ok = "term" in src or "anchor" in src
                elif provider == "GEMINI":
                    ok = "anchor" in src or "term" in src
                else:
                    ok = "anchor" in src
                asg.append({"tsg_id": c["tsg_id"], "verdict": "TRUE" if ok else "FALSE",
                            "reason": "test reason" if ok else ""})
            out.append({"element": a["element"], "assignments": asg})
        return json.dumps({"atoms": out})


def emb(text):
    v = [0.0] * 8
    for w in str(text).lower().split():
        v[hash(w) % 8] += 1.0
    return v if any(v) else [1.0] + [0.0] * 7


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
                  "pass_2p3_fact", "pass_2p3_atom_fact", "pass_2p3_time_fact", PFX, PFX + "_cache", PFX + "_llm_cache",
                  PFX + "_tg18_proposals", PFX + "_coverage", PFX + "_taxonomy"):
            cur.execute(f"DROP TABLE IF EXISTS public.{t}")
    conn.commit()
    from test_mkp_flow import Fake as P1Fake, fake_embed as p1_embed
    from test_akp_flow import Fake as P2Fake
    s1 = p1.validate_mkp_settings({**permission["mkp"], "enabled": True, "ask_user_input": False, "table": MKP_T, "export_excel": False})
    doc = next(dict(v, document_key=k) for k, v in permission["documents"].items() if v["code"].upper() == "CGSTA17")
    run = p1.MkpRun(doc, permission, paths, s1, conn, embed=p1_embed)
    p1.run_mkp_document(doc, permission, paths, s1, conn, scope_roots=["CGSTA17_S7", "CGSTA17_S9"],
                          fake_llm=P1Fake(run.drafter), embed=p1_embed)
    o2 = {"enabled": True, "mkp_table": MKP_T, "table": AKP_T, "cache_table": AKP_T + "_cache", "ask_user_input": False,
          "export_excel": False}
    for scope in ("CGSTA17_S7", "CGSTA17_S9"):
        p2.run_akp_scope(ROOT / "Law" / "Acts" / "CGST-Act.pdf", "CGSTA17", scope_key=scope, fake_llm=P2Fake(), embed=emb,
                           settings_override=o2)
    shutil.copy(ROOT / "Config" / "gst_taxonomy.json", TAX_TMP)
    yield conn, paths
    conn.close()
    TAX_TMP.unlink(missing_ok=True)


O3 = {"enabled": True, "akp_table": AKP_T, "mkp_table": MKP_T, "table_prefix": PFX, "taxonomy_file": str(TAX_TMP),
      "excel_file": "gkp_test.xlsx", "use_cache": True}


def go(scope, fake, **over):
    return p3.run_gkp_scope("CGSTA17", scope_key=scope, fake_llm=fake, embed=emb, settings_override={**O3, **over})


def q(conn, sql, *a):
    with conn.cursor() as cur:
        cur.execute(sql, a)
        out = cur.fetchall() if cur.description else []
    conn.commit()
    return out


def test_section_7_grouped_with_names_methods_and_amendments(env):
    conn, _ = env
    fake = Fake()
    c = go("CGSTA17_S7", fake)
    assert c["approved_mappings"] > 0 and c["agree"] > 0 and c["judge"] > 0 and c["amendment_rows"] > 0
    assert fake.calls["V1"] >= 1 and fake.calls["V2"] >= 1 and fake.calls["JUDGE"] >= 1
    rows = q(conn, f"""SELECT akp_element, akp_type, tg_name, tsg_id, tsg_name, status, method, akp_id, inserted_at
                       FROM {PFX} WHERE is_latest""")
    atoms = {r[0] for r in rows if r[1] == "A"}
    approved_atoms = {r[0] for r in rows if r[1] == "A" and r[5] == "APPROVED"}
    assert atoms and atoms == approved_atoms                     # every 7 atom landed somewhere (anchors exist for s.7)
    assert all(r[2] and r[4] and r[8] for r in rows if r[5] == "APPROVED")
    assert any(r[3] == "TSG01_03" for r in rows if r[5] == "APPROVED")      # 7(1)(a) -> Barter & Exchange (anchor)
    amend = [r for r in rows if r[1] == "MKP"]
    assert amend and all(r[6].startswith("AMENDMENT") for r in amend) and any(r[3] == "TSG16_06" for r in amend)
    assert not any(r[0] == "M_HEAD" for r in rows)              # headings skipped


def test_rerun_cached_no_llm(env):
    fake = Fake()
    c = go("CGSTA17_S7", fake)
    assert c["cached_mkps"] == c["mkps"] - c["skipped_non_operative"] and sum(fake.calls.values()) == 0


def test_rerun_without_cache_same_groups_keep_version(env):
    """use_cache=false asks the verifiers again; the same groups are not saved as a new version."""
    conn, _ = env
    go("CGSTA17_S7_SS1_Ca", Fake(), use_cache=False)
    v = q(conn, f"SELECT gkp_version, bool_or(is_latest) FROM {PFX} WHERE source_mkp='CGSTA17_S7_SS1_Ca#TEXT' GROUP BY 1 ORDER BY 1")
    assert v == [(1, True)]


def test_rerun_with_different_groups_is_next_version(env):
    conn, _ = env
    q(conn, f"DELETE FROM {PFX}_llm_cache RETURNING 1")                   # raw answers are reused even with use_cache=false
    go("CGSTA17_S7_SS1_Ca", Fake(mode="NOTHING"), use_cache=False)
    v = q(conn, f"SELECT gkp_version, bool_or(is_latest) FROM {PFX} WHERE source_mkp='CGSTA17_S7_SS1_Ca#TEXT' GROUP BY 1 ORDER BY 1")
    assert [x[0] for x in v] == [1, 2] and v[0][1] is False and v[1][1] is True
    go("CGSTA17_S7_SS1_Ca", Fake(), use_cache=False)                       # back to the normal groups for the next tests


def test_fact_network_and_section_sources(env):
    conn, _ = env
    from gkp_ss.candidates import CandidateFinder
    from gkp_ss.store import GkpStore
    from gkp_ss import taxonomy as tax
    _, subs = tax.load(TAX_TMP)
    store = GkpStore(conn, "public", AKP_T, PFX)
    rows = store.mkps("CGSTA17", q(conn, f"SELECT DISTINCT version_id FROM {AKP_T}")[0][0])["CGSTA17_S9_SS1#TEXT"]
    a = next(r for r in rows if r["type"] == "A")
    atom = dict(a, source_mkp="CGSTA17_S9_SS1#TEXT", family="", facts=store.atom_facts("CGSTA17", a.get("akp_id", "").split("::")[0] and
                q(conn, f"SELECT DISTINCT version_id FROM {AKP_T}")[0][0], "CGSTA17_S9_SS1#TEXT", a["element"]))
    cands = CandidateFinder(store, {x["tsg_id"]: x for x in subs}, {"max_candidates": 25}, PFX + "_taxonomy").find(
        atom, {g for g in {x["tg_id"] for x in subs} if g != "TG18"}, 5)
    srcs = " | ".join(" ".join(c["sources"]) for c in cands)
    assert "fact F_SUPPLY_CONSIDERATION" in srcs                 # atoms of s.7 test the same fact


def test_same_atom_inherits_groups(env):
    conn, _ = env
    canon = q(conn, f"""SELECT akp_id FROM {PFX} WHERE is_latest AND status='APPROVED' AND akp_type='A'
                        AND source_mkp='CGSTA17_S7_SS1_Ca#TEXT' LIMIT 1""")[0][0]
    q(conn, f"""UPDATE {AKP_T} SET type='A_REF', results='', same_akp_id=%s
               WHERE source_mkp='CGSTA17_S9_SS2#TEXT' AND type='A' AND is_latest""", canon)
    fake = Fake()
    go("CGSTA17_S9_SS2", fake)
    got = q(conn, f"""SELECT tsg_id, method FROM {PFX} WHERE source_mkp='CGSTA17_S9_SS2#TEXT' AND is_latest""")
    want = q(conn, f"SELECT tsg_id FROM {PFX} WHERE akp_id=%s AND is_latest AND status='APPROVED'", canon)
    assert got and {g[1] for g in got} == {"SAME_INHERITED"} and {g[0] for g in got} == {w[0] for w in want}
    assert sum(fake.calls.values()) == 0


def test_unclassified_proposal_approve_then_regrouped(env):
    conn, _ = env
    fake = Fake("NOTHING")
    c = go("CGSTA17_S9_SS1", fake, use_cache=False)
    assert c["unclassified"] >= 1 and c["tg18_proposals"] >= 1
    pid = q(conn, f"SELECT proposal_id FROM {PFX}_tg18_proposals WHERE status='PENDING' LIMIT 1")[0][0]
    sid = p3.approve_tg18(pid, settings_override=O3)
    assert sid.startswith("TSG18_")
    c2 = go("CGSTA17_S9_SS1", Fake("NOTHING"))                    # taxonomy changed -> re-grouped
    assert c2["tg18_mappings"] >= 1
    assert q(conn, f"SELECT status FROM {PFX}_tg18_proposals WHERE proposal_id=%s", pid)[0][0] in ("APPROVED", "COVERED")


def test_coverage_and_excel(env):
    conn, paths = env
    cov = q(conn, f"SELECT tsg_id, anchor, status FROM {PFX}_coverage")
    assert cov and any(r[2] == "OK" for r in cov)
    import openpyxl
    wb = openpyxl.load_workbook(paths["output_dir"] / "gkp_test.xlsx", read_only=True)
    assert {"Summary", "Group Mapping", "Review & Unclassified", "TG18 Proposals", "Coverage"} <= set(wb.sheetnames)


def test_governance_master_built_from_the_database(env, tmp_path):
    """GKP -> governance master on the real tables: the TSGs of the MKPs are found, built and saved."""
    import copy
    import governance_master_engine as gm
    conn, _ = env
    perm = copy.deepcopy(__import__("kp_settings").load())
    g = perm["governance_master"]
    g.update({"table": "gm_p3t", "history_table": "gm_p3t_history", "output_dir": str(tmp_path / "Governance_Master"),
              "tsgs": "TOUCHED", "use_cache": True, "save_to_db": True, "enabled": True})
    g["tables"] = {"akp": AKP_T, "fact": "pass_2p3_fact", "gkp": PFX, "time_fact": "pass_2p3_time_fact", "mkp": MKP_T,
                   "ikp": g["tables"]["ikp"]}
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.gm_p3t")
        cur.execute("DROP TABLE IF EXISTS public.gm_p3t_history")
    conn.commit()
    results = gm.run_for_mkps(["CGSTA17_S7_SS1_Ca#TEXT"], perm)
    assert results and any(r["tsg"] == "TSG01_03" for r in results), results
    r = next(r for r in results if r["tsg"] == "TSG01_03")
    assert r["status"] == "WRITTEN", r
    assert (tmp_path / "Governance_Master" / "TG01" / "TSG01_03" / "rulebook.yaml").is_file()
    assert q(conn, "SELECT count(*) FROM gm_p3t WHERE tsg_id='TSG01_03'")[0][0] > 0
    again = gm.run_for_mkps(["CGSTA17_S7_SS1_Ca#TEXT"], perm)                 # nothing changed: no new history row
    assert q(conn, "SELECT count(*) FROM gm_p3t_history WHERE tsg_id='TSG01_03'")[0][0] == 1, again
