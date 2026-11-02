"""GOVERNANCE MASTER / Excel: report.xlsx next to each governance master, and index.xlsx listing every governance master."""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

HEAD = Font(name="Arial", bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="1F4E78")
BODY = Font(name="Arial")


def _sheet(wb, title, columns, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(columns)
    for c in ws[1]:
        c.font, c.fill = HEAD, FILL
    for r in rows:
        ws.append([("" if v is None else str(v))[:32000] for v in r])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = BODY
    for i, col in enumerate(columns, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = (widths or {}).get(col, min(60, max(12, len(col) + 4)))
    ws.freeze_panes = "A2"


def write(path: Path, pack: dict, report: dict, id_map: list, problems: list, status: str) -> Path:
    """report.xlsx: why each law is in the governance master, what is inside it, and anything to review."""
    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Summary", ["Item", "Value"], [
        ("Governance master", f"{report['tsg']} {report['tsg_name']}"), ("Group", f"{report['tg']} {report['tg_name']}"),
        ("Status", status), ("Law as on", report.get("law_as_on") or "latest"),
        ("Laws in it", len(report.get("included", []))), ("Laws in the spine", len(report.get("covered_by_spine", []))),
        ("Problems found", len(problems) + len(report.get("blocking", [])))] +
        [(f"Count: {k}", n) for k, n in report.get("counts", {}).items()], {"Item": 24, "Value": 70})
    _sheet(wb, "Laws", ["MKP", "Why it is in"], [(x["mkp"], x["reason"]) for x in report.get("included", [])] +
           [(x, "IN SPINE") for x in report.get("covered_by_spine", [])] +
           [(x["mkp"], "LEFT OUT: " + x["reason"]) for x in report.get("excluded", [])], {"MKP": 36, "Why it is in": 80})
    _sheet(wb, "Molecules", ["Molecule", "MKP id", "Element type", "Element id", "Governance master id", "Concept"],
           [(m["molecule"], m["mkp_id"], m["element_type"], m["element_id"], m["rulebook_id"], m["concept"])
            for m in report.get("molecules", [])])
    _sheet(wb, "Coverage", ["Source MKP", "Atom", "Type", "State"],
           [(c["source_mkp"], c["atom"], c["type"], c["state"]) for c in report.get("coverage", [])])
    _sheet(wb, "Facts", ["Fact", "AKP type", "Governance master type", "Tier", "Evidence", "Derived from"],
           [(f["fact"], f["akp_type"], f["engine_type"], f["tier"], f["evidence"], ", ".join(map(str, f["derived_from"])))
            for f in report.get("facts", [])])
    _sheet(wb, "Ids", ["Governance master id", "AKP element", "Kind", "Source MKP", "MKP id", "AKP id"],
           [(m["rulebook_id"], m["akp_element"], m["kind"], m["source_mkp"], m["mkp_id"], m["akp_id"]) for m in id_map])
    _sheet(wb, "Review", ["Kind", "Message"], [("PROBLEM", x) for x in problems] +
           [("BLOCKING", x) for x in report.get("blocking", [])] + [("UNRESOLVED", x) for x in report.get("unresolved", [])] +
           [("WARNING", x) for x in report.get("warnings", [])], {"Kind": 14, "Message": 110})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_index(path: Path, packs: list) -> Path:
    """index.xlsx: one row per governance master."""
    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, "Governance masters", ["Sub-group", "Name", "Group", "Status", "Laws", "Changes so far", "Last changed", "File"],
           [(p["tsg"], p.get("tsg_name"), f"{p.get('tg', '')} {p.get('tg_name', '')}".strip(), p.get("pack_status"),
             len(p.get("laws") or []), p.get("version"), str(p.get("changed_at") or ""), p.get("file"))
            for p in packs], {"Sub-group": 12, "Name": 36, "Group": 34, "Status": 14, "Laws": 7, "Changes so far": 14,
                              "Last changed": 28, "File": 60})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
