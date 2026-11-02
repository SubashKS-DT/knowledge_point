"""AKP / reading model answers (same rules as the original engine)."""
from __future__ import annotations

import json
import re
from typing import Dict, List

from transport import get_field

COLS = ["MKP", "TYPE", "ELEMENT", "CONCEPT", "RESULTS"]
TYPES = {"MKP", "LE", "OR", "G", "PD", "A", "A_REF", "CT", "FACTS", "LK", "SP", "LW", "TC", "SUM"}


def rfield(row: dict, name: str) -> str:
    """A key from RESULTS only (get_field would return the TYPE / ELEMENT column for 'type' / 'element')."""
    from transport import parse_results
    return str(parse_results(row.get("RESULTS", "")).get(name.lower(), "")).strip()


def family(row: dict) -> str:
    return get_field(row, "family").strip()


def field_list(row: dict, name: str) -> List[str]:
    """'[A_X, A_Y]' -> ['A_X', 'A_Y']"""
    value = get_field(row, name).strip()
    value = value.strip("[]")
    return [v.strip().strip('"\'') for v in value.split(",") if v.strip().strip('"\'')]


def parse_rows(raw: str, mkp_id: str) -> List[dict]:
    """Five-column element table, tolerating harmless Markdown wrappers (original _parse_rows)."""
    text = (raw or "").strip()
    text = re.sub(r"^```[^\n]*\n|\n```\s*$", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = "|".join(COLS)

    def cells_of(line):
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            line = line[1:-1]
        return [cell.strip() for cell in line.split("|")]

    start = next((i for i, line in enumerate(lines) if "|".join(cells_of(line)).replace(" ", "").upper() == header), None)
    if start is not None:
        data_lines = lines[start + 1:]
    else:
        data_lines = lines
        if not data_lines or not all(len(cells_of(line)) == 5 and cells_of(line)[0] in (mkp_id, "TXN") for line in data_lines):
            raise ValueError("Missing MKP|TYPE|ELEMENT|CONCEPT|RESULTS header")
    rows = []
    for line in data_lines:
        cells = cells_of(line)
        if len(cells) == 5 and all(set(c.replace(" ", "")) <= set("-:") for c in cells):
            continue
        if len(cells) != 5:
            raise ValueError(f"Expected five columns: {line[:160]}")
        row = dict(zip(COLS, cells))
        if row["TYPE"] not in TYPES or row["MKP"] not in (mkp_id, "TXN"):
            raise ValueError(f"Invalid row TYPE/owner {row['TYPE']}/{row['MKP']}: {line[:160]}")
        if row["TYPE"] == "A" and len(row["CONCEPT"].split()) not in (2, 3):
            raise ValueError(f"Atom concept must be 2-3 words: {row['ELEMENT']}")
        if row["TYPE"] == "A" and not family(row):
            raise ValueError(f"Atom needs family in RESULTS: {row['ELEMENT']}")
        if not row["ELEMENT"] or not row["RESULTS"]:
            raise ValueError(f"Empty ELEMENT or RESULTS: {line[:160]}")
        rows.append(row)
    if not rows or not any(r["TYPE"] == "MKP" for r in rows):
        raise ValueError("MKP framing row missing")
    return rows


def parse_verdict(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Comparison did not return JSON")
    result = json.loads(text[start:end + 1])
    if result.get("verdict") not in ("TRUE", "FALSE"):
        raise ValueError("Comparison verdict must be TRUE or FALSE")
    for k in ("missing", "added_or_changed", "logic_problems", "same_matches", "references"):
        if not isinstance(result.get(k, []), list):
            raise ValueError(f"Comparison field {k} must be a JSON array")
        result.setdefault(k, [])
    return result


def table_text(rows: List[dict]) -> str:
    return "MKP|TYPE|ELEMENT|CONCEPT|RESULTS\n" + "\n".join("|".join(r.get(c, "") for c in COLS) for r in rows)


def raw_rows(raw: str, mkp_id: str) -> List[dict]:
    """Best-effort candidate rows for Excel (never blocks judging)."""
    try:
        return parse_rows(raw, mkp_id)
    except (ValueError, TypeError):
        raw = raw or ""
        return [{"MKP": mkp_id, "TYPE": "RAW", "ELEMENT": f"RAW_{i + 1}", "CONCEPT": "",
                 "RESULTS": raw[i * 30000:(i + 1) * 30000]} for i in range((len(raw) + 29999) // 30000)]
