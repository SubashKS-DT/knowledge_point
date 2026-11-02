"""IKP / LLM assist - used ONLY for a chapter whose Python quality gate failed
(or every chapter when ikp.llm.mode = "ALWAYS").

The model sees the chapter as numbered PDF lines and returns where each unit STARTS
(line id + character position + level + number + parent). It never returns legal text.
Python checks every tag against the printed characters, rebuilds the chapter from the
accepted tags, re-runs the quality gate, and keeps whichever version (Python or LLM-assisted)
scores better. Responses are cached, so a re-run or resume costs nothing.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .structure import HEADING_DELIM, Node

LEVELS = {"SECTION", "RULE", "ENTRY", "PARAGRAPH", "SUB_PARAGRAPH", "SUB_SECTION", "SUB_RULE", "CLAUSE",
          "SUB_CLAUSE", "ITEM", "PROVISO", "EXPLANATION", "ILLUSTRATION", "CLOSING", "TABLE", "TABLE_ROW",
          "ANNEXURE", "NOTE", "HISTORICAL", "ORDER", "SUBJECT", "SIGNATURE"}
PREFIX = {"SUB_SECTION": "SS", "SUB_RULE": "SR", "CLAUSE": "C", "SUB_CLAUSE": "SC", "ITEM": "I", "PROVISO": "P",
          "EXPLANATION": "E", "ILLUSTRATION": "ILL", "CLOSING": "CL", "TABLE": "TBL", "TABLE_ROW": "ROW",
          "ANNEXURE": "ANX", "NOTE": "NOTE", "HISTORICAL": "HIST", "ORDER": "O", "SUB_PARAGRAPH": "SP",
          "ENTRY": "EN"}
KIND = {"SECTION": "SECTION", "RULE": "SECTION", "PARAGRAPH": "SECTION", "ENTRY": "ENTRY", "SUB_SECTION": "NUM",
        "SUB_RULE": "NUM", "CLAUSE": "ALPHA", "SUB_CLAUSE": "ROMAN", "ITEM": "UPPER"}


@dataclass
class AssistReport:
    called: bool = False
    cached: bool = False
    accepted: int = 0
    rejected: List[str] = field(default_factory=list)
    doubts: List[dict] = field(default_factory=list)
    calls: int = 0
    error: str = ""
    agreement: dict = field(default_factory=dict)
    gate_before: str = ""
    gate_after: str = ""


# ---------------------------------------------------------------- helpers

def _group_span(a, group) -> Tuple[int, int]:
    nodes = a.parsed.nodes
    if group.level == "BODY":
        return min(nodes[k].start for k in group.keys if nodes[k].start >= 0), max(nodes[k].end for k in group.keys)
    root = nodes[group.key]
    return root.start, root.end


def _line_range(a, group) -> Tuple[int, int]:
    doc = a.parsed
    lo, hi = _group_span(a, group)
    i = max(bisect.bisect_right(doc.line_offsets, lo) - 1, 0)
    j = bisect.bisect_left(doc.line_offsets, hi)
    ids = [doc.seg_line[k] for k in range(i, min(j, len(doc.seg_line))) if doc.seg_line[k] >= 0]
    return (min(ids), max(ids)) if ids else (0, -1)


def _offset_to_line(a, offset: int) -> Tuple[int, int]:
    doc = a.parsed
    i = max(bisect.bisect_right(doc.line_offsets, offset) - 1, 0)
    return doc.seg_line[i], doc.seg_line_off[i] + (offset - doc.line_offsets[i])


def _line_to_offset(a, line: int, at: int) -> Optional[int]:
    doc = a.parsed
    best = None
    for i, li in enumerate(doc.seg_line):
        if li == line and doc.seg_line_off[i] <= at:
            seg_end = doc.line_offsets[i + 1] if i + 1 < len(doc.line_offsets) else len(doc.stream)
            if best is None or doc.seg_line_off[i] >= doc.seg_line_off[best]:
                best = i
            del seg_end
        elif li > line and best is not None:
            break
    if best is None:
        return None
    return doc.line_offsets[best] + (at - doc.seg_line_off[best])


def python_draft(a, group) -> List[dict]:
    nodes = a.parsed.nodes
    root_keys = {group.key} if group.level != "BODY" else set()
    starts, ref_of = [], {}
    for k in group.keys:
        if k in root_keys:
            continue
        n = nodes[k]
        if not n.ranges:
            continue
        line, at = _offset_to_line(a, n.ranges[0][0])
        ref = f"L{line}" if at == 0 else f"L{line}@{at}"
        ref_of[k] = ref
        starts.append({"line": f"L{line}", "at": at, "level": n.level, "number": n.number,
                       "parent": ref_of.get(n.parent)})
    return starts


def build_prompt(base: str, a, group, gate_issues: List[str]) -> str:
    lo, hi = _line_range(a, group)
    lines = a.layout.lines
    body = "\n".join(f"L{i} | p{lines[i].page} | x{lines[i].x0:.0f} | {'B' if lines[i].bold_start else ' '} | {lines[i].text}"
                     for i in range(lo, hi + 1))
    unit = "R" if a.doc_type == "RULES" else "S"
    ctx = {"instrument_code": a.code, "document_type": a.doc_type, "chapter_key": group.key,
           "chapter_heading": group.label, "key_format": f"{a.code}_{unit}<n>_{'SR' if unit == 'R' else 'SS'}<n>_C<a>_SC<i>",
           "python_gate_issues": gate_issues}
    return (f"{base}\n\n=== CONTEXT ===\n{json.dumps(ctx, ensure_ascii=False)}\n\n=== PYTHON_DRAFT ===\n"
            f"{json.dumps({'starts': python_draft(a, group)}, ensure_ascii=False)}\n\n=== LINES ===\n{body}\n=== END ===\n")


def parse_response(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", raw.strip())
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM returned no JSON object")
    data = json.loads(m.group(0))
    if not isinstance(data.get("starts"), list):
        raise ValueError("LLM JSON has no 'starts' list")
    return data


def _ref(value) -> Optional[Tuple[int, int]]:
    if value in (None, "", "null"):
        return None
    m = re.fullmatch(r"L(\d+)(?:@(\d+))?", str(value).strip())
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (-1, -1)


def _matches(level: str, number: str, text: str) -> bool:
    t = text.lstrip("[ \t")
    n = re.escape(str(number).strip("() ."))
    if level == "PARAGRAPH" and not re.match(r"^\d", t):
        return bool(t.strip())  # unnumbered first paragraph of a circular (implicit number)
    if level in ("SECTION", "RULE", "ENTRY", "PARAGRAPH", "TABLE_ROW"):
        return bool(re.match(rf"^(?:Rule\s+|Section\s+)?{n}\s*[.)]?(?!\d)", t, re.I))
    if level == "SUB_PARAGRAPH":
        return t.startswith(str(number))
    if level in ("SUB_SECTION", "SUB_RULE", "CLAUSE", "SUB_CLAUSE", "ITEM"):
        return bool(re.match(rf"^\(\s*{n}\s*\)", t))
    checks = {"PROVISO": r"^Provided\b", "EXPLANATION": r"^Explanation", "ILLUSTRATION": r"^Illustration",
              "ANNEXURE": r"^ANNEX", "NOTE": r"^NOTE", "ORDER": r"^Order", "SUBJECT": r"^Sub"}
    return bool(re.match(checks[level], t, re.I)) if level in checks else bool(t.strip())


def validate(a, group, data: dict) -> Tuple[List[dict], List[str]]:
    lo, hi = _line_range(a, group)
    lines = a.layout.lines
    accepted, rejected, seen = [], [], set()
    last = (-1, -1)
    for tag in data.get("starts", []):
        try:
            ref = _ref(tag.get("line"))
            line = ref[0]
            at = int(tag.get("at") or 0)
            level = str(tag.get("level", "")).upper().strip()
            number = str(tag.get("number", "")).strip().strip("()").rstrip(".")
        except Exception:
            rejected.append(f"unreadable tag {tag}")
            continue
        why = None
        if level not in LEVELS:
            why = f"unknown level {level}"
        elif not (lo <= line <= hi):
            why = f"line L{line} outside chapter"
        elif not (0 <= at < len(lines[line].text)):
            why = f"position {at} outside line L{line}"
        elif (line, at) <= last:
            why = "not in reading order"
        elif not _matches(level, number, lines[line].text[at:]):
            why = f"printed text at L{line}@{at} does not start with {level} {number}"
        else:
            parent = _ref(tag.get("parent"))
            if parent is not None and parent not in seen:
                why = f"parent {tag.get('parent')} is not an earlier accepted start"
        if why:
            rejected.append(f"{tag.get('line')}@{tag.get('at', 0)} {level} {number}: {why}")
            continue
        seen.add((line, at))
        last = (line, at)
        accepted.append({"line": line, "at": at, "level": level, "number": number or "1", "parent": _ref(tag.get("parent"))})
    return accepted, rejected


def apply(a, group, tags: List[dict]) -> None:
    """Replace the chapter's sub-tree with the one described by the accepted tags."""
    doc = a.parsed
    nodes = doc.nodes
    code = a.code
    body = group.level == "BODY"
    root = nodes[code] if body else nodes[group.key]
    span_start, span_end = _group_span(a, group)
    removed = [k for k in group.keys if (body or k != group.key)]
    removed_set = set(removed)
    insert_at = doc.order.index(removed[0]) if removed else doc.order.index(root.key) + 1
    for k in removed:
        n = nodes.pop(k)
        if n.parent in nodes and k in nodes[n.parent].children:
            nodes[n.parent].children.remove(k)
    doc.order = [k for k in doc.order if k not in removed_set]
    doc.issue_keys = [x for x in doc.issue_keys if x[0] not in removed_set]
    offsets = [_line_to_offset(a, t["line"], t["at"]) for t in tags]
    pairs = [(t, o) for t, o in zip(tags, offsets) if o is not None and span_start <= o < span_end]
    if not body and pairs:
        root.ranges = [[span_start, pairs[0][1]]] if pairs[0][1] > span_start else []
    by_ref: Dict[Tuple[int, int], str] = {}
    new_keys = []
    counters: Dict[Tuple[str, str], int] = {}
    for idx, (t, off) in enumerate(pairs):
        end = pairs[idx + 1][1] if idx + 1 < len(pairs) else span_end
        parent_key = by_ref.get(t["parent"]) if t["parent"] else root.key
        if parent_key is None:
            parent_key = root.key
        level, number = t["level"], t["number"]
        if level in ("SECTION", "RULE"):
            key = f"{code}_{'R' if level == 'RULE' else 'S'}{number}"
        elif level == "PARAGRAPH":
            key = f"{parent_key}_PA{number}" if parent_key != code else f"{code}_PA{number}"
        elif level in ("SUBJECT", "SIGNATURE"):
            key = f"{code}_{level}"
        else:
            if level in ("PROVISO", "EXPLANATION", "CLOSING", "TABLE", "NOTE", "ILLUSTRATION", "ANNEXURE", "HISTORICAL",
                         "ORDER"):
                c = counters.get((parent_key, level), 0) + 1
                counters[(parent_key, level)] = c
                number = number if level == "EXPLANATION" and number not in ("", "1") else str(c)
            key = f"{parent_key}_{PREFIX.get(level, level)}{number}"
        base, n = key, 2
        while key in nodes:
            key = f"{base}_{n}"
            n += 1
        line = a.layout.lines[t["line"]]
        text = line.text[t["at"]:]
        heading = ""
        if level in ("SECTION", "RULE", "PARAGRAPH", "ENTRY"):
            rest = re.sub(r"^[\[\s]*(?:Rule\s+|Section\s+)?[\dA-Z]+\s*\.?\s*", "", text)
            dm = HEADING_DELIM.search(rest[:200])
            heading = rest[:dm.start()].strip(" .[") if dm else (rest[:80].strip() if level == "ENTRY" else "")
        node = Node(key, level, number=number, parent=parent_key, kind=KIND.get(level, level), heading=heading,
                    marker_x=line.x0, bracketed=text.lstrip().startswith("["), flags=["LLM_ASSIST"])
        node.ranges = [[off, end]]
        nodes[key] = node
        nodes[parent_key].children.append(key)
        by_ref[(t["line"], t["at"])] = key
        new_keys.append(key)
    doc.order[insert_at:insert_at] = new_keys
    for i, k in enumerate(doc.order):
        nodes[k].order = i
    for k in reversed(doc.order):  # recompute subtree spans
        n = nodes[k]
        starts = [r[0] for r in n.ranges] + [nodes[c].start for c in n.children if nodes[c].start >= 0]
        ends = [r[1] for r in n.ranges] + [nodes[c].end for c in n.children if nodes[c].end >= 0]
        n.start = min(starts) if starts else -1
        n.end = max(ends) if ends else -1


def assist_group(a, group, base_prompt: str, call: Callable[[str], str], gate: Callable, regroup: Callable,
                 re_enrich: Callable, cache_get: Callable[[str], Optional[str]], cache_put: Callable[[str, str], None],
                 model_id: str, gate_issues: List[str]) -> Tuple[object, AssistReport, object]:
    """Returns (best group, report, best gate result). `a` is modified only if the LLM version wins."""
    report = AssistReport()
    before = gate(a, group)
    prompt = build_prompt(base_prompt, a, group, gate_issues)
    cache_key = hashlib.sha256((model_id + "\n" + prompt).encode("utf-8")).hexdigest()
    raw = cache_get(cache_key)
    report.cached = raw is not None
    if raw is None:
        try:
            raw = call(prompt)
            report.calls = 1
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            return group, report, before
        cache_put(cache_key, raw)
    report.called = True
    try:
        data = parse_response(raw)
    except Exception as exc:
        report.error = f"bad JSON: {exc}"
        return group, report, before
    tags, rejected = validate(a, group, data)
    report.accepted, report.rejected, report.doubts = len(tags), rejected, data.get("doubts", [])
    draft = {(int(t["line"][1:]), t["at"], t["level"], str(t["number"])) for t in python_draft(a, group)}
    mine = {(t["line"], t["at"], t["level"], str(t["number"])) for t in tags}
    report.agreement = {"python_starts": len(draft), "llm_starts": len(mine), "same": len(draft & mine),
                        "llm_only": sorted(f"L{l}@{at} {lv} {n}" for l, at, lv, n in mine - draft)[:40],
                        "python_only": sorted(f"L{l}@{at} {lv} {n}" for l, at, lv, n in draft - mine)[:40]}
    if not tags:
        return group, report, before
    saved = (copy.deepcopy(a.parsed), copy.deepcopy(a.enriched))
    apply(a, group, tags)
    re_enrich(a)
    new_group = regroup(a, group.key)
    after = gate(a, new_group)
    rank = {"FAIL": 0, "WARN": 1, "PASS": 2}
    better = (rank[after.status], -len(after.issues)) > (rank[before.status], -len(before.issues))
    report.gate_before, report.gate_after = before.status + " " + "; ".join(before.issues), after.status + " " + "; ".join(after.issues)
    if not better:
        a.parsed, a.enriched = saved
        report.error = report.error or f"LLM version not better ({after.status} vs {before.status}); Python kept"
        return group, report, before
    return new_group, report, after
