"""GKP / Excel: Summary, Group Mapping, Review & Unclassified, TG18 Proposals, Coverage (from the database)."""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from pathlib import Path

G_COLS = ["Document", "Version", "Source MKP", "MKP_ID", "AKP_ID", "AKP Element", "AKP Type", "Concept", "Group ID",
          "Group Name", "Subgroup ID", "Subgroup Name", "Status", "Method", "Concept Similarity", "Atom Similarity",
          "Reason", "Verifier 1", "Verifier 2", "GKP Version", "Inserted At"]
P_COLS = ["Document", "Version", "Source MKP", "AKP Element", "Proposal ID", "Proposed Subgroup", "Definition",
          "Source Evidence", "Reason", "Review Status", "Approved Subgroup"]
C_COLS = ["Document", "Version", "Subgroup ID", "Anchor Provision", "Atoms In Provision", "Atoms Approved In Subgroup",
          "Status", "Checked At"]


def write(path: Path, store, by_id: dict) -> Path:
    import xlsxwriter
    g, p, c = store.export()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    approved = [r for r in g if r[12] == "APPROVED"]
    other = [r for r in g if r[12] != "APPROVED"]
    atoms_all = {(r[0], r[2], r[5]) for r in g if r[6] != "MKP"}
    atoms_ok = {(r[0], r[2], r[5]) for r in approved if r[6] != "MKP"}
    fd, tmp = tempfile.mkstemp(prefix="gkp_", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        with xlsxwriter.Workbook(tmp) as wb:
            hdr = wb.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#17365D", "text_wrap": True})
            cell = wb.add_format({"text_wrap": True, "valign": "top"})

            def sheet(name, cols, rows, widths):
                ws = wb.add_worksheet(name)
                ws.freeze_panes(1, 0)
                for i, h in enumerate(cols):
                    ws.write(0, i, h, hdr)
                for r, row in enumerate(rows, 1):
                    for i, v in enumerate(row):
                        ws.write(r, i, "" if v is None else (v if isinstance(v, (int, float)) else str(v)[:32000]), cell)
                for i, w in enumerate(widths):
                    ws.set_column(i, i, w)
                ws.autofilter(0, 0, max(1, len(rows)), len(cols) - 1)
            summary = [("AKPs grouped", len(atoms_all)), ("AKPs with an approved subgroup", len(atoms_ok)),
                       ("AKPs with no approved subgroup", len(atoms_all - atoms_ok)),
                       ("Approved memberships", len(approved)),
                       ("  - both verifiers agreed", sum(1 for r in approved if r[13] == "AGREE")),
                       ("  - decided by judge", sum(1 for r in approved if r[13] == "JUDGE")),
                       ("  - inherited from SAME atom", sum(1 for r in approved if r[13] == "SAME_INHERITED")),
                       ("  - amendment rule / target", sum(1 for r in approved if r[13].startswith("AMENDMENT"))),
                       ("REVIEW rows", sum(1 for r in other if r[12] == "REVIEW")),
                       ("UNCLASSIFIED rows", sum(1 for r in other if r[12] == "UNCLASSIFIED")),
                       ("TG18 proposals pending", sum(1 for x in p if x[9] == "PENDING")),
                       ("Coverage gaps (anchor provision not in its subgroup)", sum(1 for x in c if x[6] == "GAP"))]
            per_sub = Counter((r[8], r[9], r[10], r[11]) for r in approved)
            rows = [list(x) for x in summary] + [[], ["Group ID", "Group", "Subgroup ID", "Subgroup", "Approved memberships"]]
            seen = set()
            for sid, sub in sorted(by_id.items()):
                n = per_sub.get((sub["tg_id"], sub["tg_name"], sid, sub["name"]), 0)
                rows.append([sub["tg_id"], sub["tg_name"], sid, sub["name"], n])
                seen.add(sid)
            sheet("Summary", ["Metric", "Count", "", "", ""], rows, (42, 30, 12, 40, 12))
            sheet("Group Mapping", G_COLS, approved, (10, 18, 30, 16, 36, 12, 8, 26, 8, 22, 10, 30, 12, 15, 9, 9, 60, 9, 9, 7, 19))
            sheet("Review & Unclassified", G_COLS, other, (10, 18, 30, 16, 36, 12, 8, 26, 8, 22, 10, 30, 12, 15, 9, 9, 60, 9, 9, 7, 19))
            sheet("TG18 Proposals", P_COLS, p, (10, 18, 30, 12, 16, 30, 60, 40, 40, 12, 12))
            sheet("Coverage", C_COLS, c, (10, 18, 12, 30, 10, 12, 8, 19))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
