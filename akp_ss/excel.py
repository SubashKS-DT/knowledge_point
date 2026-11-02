"""AKP / Excel: one workbook, per document the three sheets Gemini / Claude / Claude STD (as MKP),
plus Facts and Time facts. Rebuilt from the database (the source of truth) every `excel_every` MKPs and at the end."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

COLS = ["MKP", "MKP_ID", "TYPE", "ELEMENT", "CONCEPT", "RESULTS"]


def write(path: Path, store, facts_conn_rows, short_names: dict) -> Path:
    import xlsxwriter
    std, cache, times = store.export_rows()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    by_doc = {}
    for inst, src, mkp_id, owner, kind, el, concept, results, ver, at, aid, same in std:
        by_doc.setdefault(inst, {"GEMINI": [], "CLAUDE": [], "STANDARD": []})["STANDARD"].append(
            {"MKP": owner, "MKP_ID": mkp_id or "", "TYPE": kind, "ELEMENT": el, "CONCEPT": concept, "RESULTS": results,
             "AKP_ID": aid or "", "SAME_AKP_ID": same or "", "AKP_VERSION": ver, "INSERTED_AT": str(at)[:19]})
    ids = {(r[0], r[1]): r[2] for r in std}
    for inst, src, data in cache:
        d = by_doc.setdefault(inst, {"GEMINI": [], "CLAUDE": [], "STANDARD": []})
        mkp_id = ids.get((inst, src), "")
        for key in ("GEMINI", "CLAUDE"):
            for row in (data or {}).get(key.lower(), []) or []:
                if isinstance(row, dict):
                    d[key].append({**row, "MKP_ID": row.get("MKP_ID") or mkp_id})
    fd, tmp = tempfile.mkstemp(prefix="akp_", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        with xlsxwriter.Workbook(tmp) as wb:
            hdr = wb.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#16324F", "valign": "vcenter"})
            cell = wb.add_format({"text_wrap": True, "valign": "top"})

            def sheet(name, cols, rows, widths):
                ws = wb.add_worksheet(re.sub(r"[\[\]:*?/\\]", "-", name)[:31])   # Excel: no [ ] : * ? / \ , max 31
                ws.freeze_panes(1, 0)
                for c, label in enumerate(cols):
                    ws.write(0, c, label, hdr)
                for i, row in enumerate(rows, 1):
                    for c, label in enumerate(cols):
                        ws.write_string(i, c, str(row.get(label, "") if row.get(label) is not None else "")[:32000], cell)
                for c, w in enumerate(widths):
                    ws.set_column(c, c, w)
                ws.autofilter(0, 0, max(1, len(rows)), len(cols) - 1)
            for inst in sorted(by_doc):
                short = short_names.get(inst, inst)
                d = by_doc[inst]
                sheet(f"Gemini {short}", COLS, d["GEMINI"], (22, 18, 10, 34, 26, 90))
                sheet(f"Claude {short}", COLS, d["CLAUDE"], (22, 18, 10, 34, 26, 90))
                sheet(f"Claude STD {short}", COLS + ["AKP_ID", "SAME_AKP_ID", "AKP_VERSION", "INSERTED_AT"], d["STANDARD"],
                      (22, 18, 10, 34, 26, 90, 34, 34, 8, 19))
            fcols = ["FACT_ID", "NAME", "ENTITY", "TYPE", "UNIT", "OPTIONS", "EVIDENCE", "USED_BY_ATOMS", "POSSIBLE_DUPLICATE_OF",
                     "FIRST_SOURCE_MKP"]
            sheet("Facts", fcols, [dict(zip(fcols, r)) for r in facts_conn_rows], (30, 30, 12, 9, 8, 30, 30, 9, 22, 30))
            tcols = ["INSTRUMENT", "AMENDMENT_MKP", "TARGET", "KIND", "AMENDED_BY", "INSTRUMENT_DATE", "EFFECTIVE_FROM",
                     "REPLACED_TEXT", "NOTE"]
            sheet("Time facts", tcols, [dict(zip(tcols, r)) for r in times], (10, 30, 26, 12, 30, 12, 12, 40, 80))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
