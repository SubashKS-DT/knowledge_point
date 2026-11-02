"""permission.json + Config/settings.json + the Law sub-folders (no database needed)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import kp_settings as K  # noqa: E402

PERM = json.loads((ROOT / "permission.json").read_text(encoding="utf-8"))
SETTINGS = json.loads((ROOT / "Config" / "settings.json").read_text(encoding="utf-8"))


def test_codes_from_file_names():
    assert K._derive("Circular_47_21_2018-GST.pdf", "CIRCULAR")["code"] == "C47_21_2018"
    assert K._derive("11_2017-Central Tax_Rate.pdf", "NOTIFICATION") == {
        "name": "Notification 11/2017-Central Tax (Rate)", "code": "N11_2017"}
    assert K._derive("CGST-Rules-2017-Part-B.pdf", "RULES") == {"name": "CGST Rules Part B", "code": "CGSTR17B"}
    assert K._derive("UTGST-Act-2017.pdf", "ACT")["code"] == "UTGSTA17"


def test_documents_from_sub_folders_and_registry(tmp_path):
    law = tmp_path / "Law"
    (law / "Acts").mkdir(parents=True)
    (law / "Circulars").mkdir()
    (law / "Acts" / "CGST-Act.pdf").write_bytes(b"%PDF")
    (law / "Circulars" / "Circular_47_21_2018-GST.pdf").write_bytes(b"%PDF")
    reg = tmp_path / "documents.json"
    reg.write_text(json.dumps({"CGST-Act.pdf": {"name": "CGST Act", "code": "CGSTA17", "type": "ACT"}}))
    st = dict(SETTINGS, paths=dict(SETTINGS["paths"], law_dir=str(law)), files=dict(SETTINGS["files"], documents_registry=str(reg)))
    docs = K.discover_documents(dict(PERM, read={"acts": True, "circulars": False}), st)
    assert docs["CGSTA17"]["read"] is True and docs["CGSTA17"]["file"] == "Acts/CGST-Act.pdf"     # code kept
    assert docs["C47_21_2018"]["read"] is False
    assert "Circular_47_21_2018-GST.pdf" in json.loads(reg.read_text())                          # added to the registry


def test_llm_role_can_switch_provider():
    perm = json.loads(json.dumps(PERM))
    perm["llm"]["mkp"]["SIMPLE"]["generator_1"] = {"llm": "claude", "model": "claude-sonnet-5-5"}
    cfg = K.load(permission=perm, settings=SETTINGS)
    assert cfg["mkp"]["models"]["SIMPLE"]["generator_1"] == {"llm": "CLAUDE", "model": "claude-sonnet-5-5"}
    perm["llm"]["mkp"]["SIMPLE"]["judge"] = {"llm": "OPENAI", "model": "x"}
    with pytest.raises(K.SettingsError):
        K.load(permission=perm, settings=SETTINGS)


def test_kind_guessed_from_file_name():
    assert K.guess_type("Circular_47_21_2018-GST.pdf") == "CIRCULAR"
    assert K.guess_type("CGST-Rules-2017-Part-B.pdf") == "RULES"
    assert K.guess_type("12_2017-Central Tax_Rate.pdf") == "NOTIFICATION"
    assert K.guess_type("IGST-Act.pdf") == "ACT"
    assert K.guess_type("readme.pdf") == ""


def test_db_upgrade_sql_takes_the_schema_parameter():
    import db_migrate
    sql = db_migrate.MIGRATION_SQL % {"schema": "'public'"}      # psycopg2 fills %(schema)s the same way
    assert "'^pass0_index$', 'ikp'" in sql and "'^ikp_index$', 'ikp'" in sql and "%(schema)s" not in sql
