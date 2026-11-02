"""IKP / Step 4 - quality gates. A document that fails a gate is reported, never silently
passed to MKP."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from .enrich import Enriched
from .layout import LayoutDocument
from .structure import ParsedDocument, num_key, roman_value

UNIT_LEVELS = ("SECTION", "RULE", "PARAGRAPH")


@dataclass
class QualityReport:
    status: str  # PASS / WARN / FAIL
    metrics: Dict[str, object] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    review: List[str] = field(default_factory=list)  # node keys a human should look at


def _gaps(numbers: List[str]) -> List[str]:
    missing = []
    ints = [num_key(n)[0] for n in numbers]
    for a, b in zip(ints, ints[1:]):
        if b - a > 1:
            missing.extend(str(x) for x in range(a + 1, b))
    return missing


def evaluate(doc: ParsedDocument, layout: LayoutDocument, enriched: Enriched) -> QualityReport:
    issues: List[str] = []
    review: List[str] = []
    nodes = doc.nodes
    units = [nodes[k] for k in doc.order if nodes[k].level in UNIT_LEVELS and nodes[k].role != "NON_OPERATIVE"]
    chapters = [nodes[k] for k in doc.order if nodes[k].level == "CHAPTER"]

    # 1. text coverage: every body character must land in the stream exactly once
    body_chars = sum(len(re.sub(r"\s", "", l.text)) for l in layout.lines)
    stream_chars = len(re.sub(r"\s", "", doc.stream))
    coverage = round(100.0 * stream_chars / body_chars, 2) if body_chars else 0.0
    operative_chars = sum(len(re.sub(r"\s", "", doc.own_text(k))) for k in doc.order
                          if nodes[k].role not in ("NON_OPERATIVE", "HISTORICAL"))
    if abs(coverage - 100) > 0.5:
        issues.append(f"Text coverage {coverage}% (expected 100%)")

    # 2. numbering of sections / rules / paragraphs
    unit_numbers = [u.number for u in units if u.level != "HISTORICAL"]
    missing_units = _gaps(unit_numbers)
    if missing_units:
        issues.append(f"Unit numbers missing from sequence: {', '.join(missing_units[:30])}")
    # 3. chapters strictly increasing
    rom = [roman_value(c.number) for c in chapters]
    if rom != sorted(rom) or len(set(rom)) != len(rom):
        issues.append("Chapter numbers are not strictly increasing")
    missing_ch = [str(x) for a, b in zip(rom, rom[1:]) for x in range(a + 1, b)]
    if missing_ch:
        issues.append(f"Chapter numbers missing: {missing_ch}")

    # 4. duplicate keys / out-of-sequence markers / empty leaves
    dup = [k for k, n in nodes.items() if "DUPLICATE_KEY_SUFFIXED" in n.flags and n.role != "HISTORICAL"]
    oos = [e for e in doc.events if "out of sequence" in e]
    empty = [k for k in doc.order if not nodes[k].children and nodes[k].role == "PROVISION"
             and len(re.sub(r"^\[?\s*(\(\s*\w{1,7}\s*\)|\d{1,3}[A-Z]{0,3}\s*\.)\s*", "", doc.own_text(k)).strip()) < 2]
    if dup:
        issues.append(f"{len(dup)} duplicate structural keys (suffixed)")
    if oos:
        issues.append(f"{len(oos)} list markers out of sequence (kept as text, see review)")
    for k in doc.order:
        n = nodes[k]
        if any(f.startswith(("MARKER_OUT_OF_SEQUENCE", "PROVISO_ATTACH_REVIEW", "EXPLANATION_ATTACH_REVIEW",
                             "DUPLICATE_KEY", "OUT_OF_SEQUENCE_VERSION")) for f in n.flags):
            review.append(k)
    review.extend(e for e in empty if e not in review)

    # 5. footnote markers that never resolved
    if enriched.unresolved_markers:
        issues.append(f"{len(enriched.unresolved_markers)} footnote markers without footnote text")

    # 6. unresolved internal references (typo in law or parser miss) are informative only
    internal = [x for x in enriched.xrefs if not x.external]
    unresolved = [x for x in internal if not x.resolved]

    metrics = {
        "pages": layout.page_count,
        "toc_pages": layout.toc_pages,
        "dropped_header_footer_lines": sum(1 for d in layout.dropped if d[1] == "HEADER_FOOTER"),
        "body_lines": len(layout.lines),
        "footnotes": len(layout.footnotes),
        "margin_notes": len(layout.margin_notes),
        "nodes": len(nodes) - 1,
        "chapters": len(chapters),
        "schedules": sum(1 for n in nodes.values() if n.level == "SCHEDULE"),
        "units": len(units),
        "first_unit": unit_numbers[0] if unit_numbers else "",
        "last_unit": unit_numbers[-1] if unit_numbers else "",
        "text_coverage_pct": coverage,
        "operative_chars": operative_chars,
        "out_of_sequence_markers": len(oos),
        "duplicate_keys": len(dup),
        "empty_provisions": len(empty),
        "xrefs": len(enriched.xrefs),
        "xrefs_internal_resolved_pct": round(100.0 * (len(internal) - len(unresolved)) / len(internal), 1) if internal else 100.0,
        "definitions": len(enriched.definitions),
        "unresolved_footnote_markers": len(enriched.unresolved_markers),
        "historical_versions": sum(1 for n in nodes.values() if n.level == "HISTORICAL"),
    }
    if not units:
        status = "FAIL"
        issues.insert(0, "No sections / rules / paragraphs were found")
    elif missing_ch or abs(coverage - 100) > 2 or len(missing_units) > max(3, 0.05 * len(units)):
        status = "FAIL"
    elif issues:
        status = "WARN"
    else:
        status = "PASS"
    return QualityReport(status, metrics, issues, review)


GROUP_UNIT_LEVELS = ("SECTION", "RULE", "ENTRY", "PARAGRAPH")
RANK = {"FAIL": 0, "WARN": 1, "PASS": 2}


def evaluate_group(doc: ParsedDocument, group) -> QualityReport:
    """Quality gate for ONE chapter / schedule / annex / body, run before that chapter is saved."""
    nodes = doc.nodes
    keys = set(group.keys)
    issues: List[str] = []
    review: List[str] = []
    units = [nodes[k] for k in group.keys if nodes[k].level in GROUP_UNIT_LEVELS and (nodes[k].parent in keys or nodes[k].parent == doc.code)]
    numbers = [u.number for u in units]
    missing = _gaps(numbers)
    oos = [(k, p, m) for k, p, m in doc.issue_keys if k in keys]
    empty = [k for k in group.keys if not nodes[k].children and nodes[k].role == "PROVISION"
             and len(re.sub(r"^\[?\s*(\(\s*\w{1,7}\s*\)|\d{1,3}[A-Z]{0,3}\s*\.)\s*", "", doc.own_text(k)).strip()) < 2]
    dup = [k for k in group.keys if "DUPLICATE_KEY_SUFFIXED" in nodes[k].flags and nodes[k].level != "HISTORICAL"]
    if group.level in ("CHAPTER", "SCHEDULE") and not units:
        issues.append(f"{group.level.lower()} has no sections/rules/entries")
    if missing:
        issues.append(f"numbers missing inside this {group.level.lower()}: {', '.join(missing[:20])}")
    if oos:
        issues.append(f"{len(oos)} list markers out of sequence")
    if empty:
        issues.append(f"{len(empty)} empty provisions")
    if dup:
        issues.append(f"{len(dup)} duplicate keys")
    for k in group.keys:
        if any(f.startswith(("MARKER_OUT_OF_SEQUENCE", "PROVISO_ATTACH_REVIEW", "EXPLANATION_ATTACH_REVIEW",
                             "DUPLICATE_KEY")) for f in nodes[k].flags) or k in empty:
            review.append(k)
    n_nodes = max(1, len(group.keys))
    if (group.level in ("CHAPTER", "SCHEDULE") and not units) or len(missing) > 2 or len(oos) > max(3, 0.05 * n_nodes) \
            or len(empty) > max(2, 0.03 * n_nodes):
        status = "FAIL"
    elif issues:
        status = "WARN"
    else:
        status = "PASS"
    metrics = {"nodes": len(group.keys), "units": len(units), "first_unit": numbers[0] if numbers else "",
               "last_unit": numbers[-1] if numbers else "", "out_of_sequence": len(oos), "empty": len(empty),
               "missing_numbers": missing[:50]}
    return QualityReport(status, metrics, issues, review)
