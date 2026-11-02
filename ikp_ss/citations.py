"""Citation parsing and resolution - shared by cross-reference detection and the MKP lookup.

Handles single, plural, list and range forms:
    section 16(2)(c)                     clause (c) of sub-section (2) of section 16
    sub-sections (1), (1A) and (2)       sections 73 to 76          clauses (a) to (d)
    sub-sections (2) and (10) of section 73       rule 36(4)       Schedule II para 5(b)
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

TOKEN = r"(?:\(\s*\w{1,5}\s*\)|\d{1,3}[A-Z]{0,2}|[IVX]{1,4})"
JOIN = r"\s*(?:,|and|or|to|&)\s*"
KIND = (r"sub-sections?|sub-rules?|sub-clauses?|clauses?|sections?|rules?|Schedules?|paragraphs?|paras?|items?|"
        r"entry|entries|provisos?|Explanations?")
CHAIN = r"(?:\s*\(\s*\w{1,5}\s*\))*"
TAIL = (r"(?:\s*(?:,\s*)?of\s+(?:the\s+)?(?:first\s+|second\s+|third\s+)?(?:sub-section|sub-rule|clause|sub-clause|"
        r"paragraph|para|proviso|Explanation)\s*(?:\(\s*\w{1,5}\s*\)|\d{0,2}))*"
        r"(?:\s*(?:,\s*)?of\s+(?:section|rule|Schedule)\s+(?:\d{1,3}[A-Z]{0,2}|[IVX]{1,4})(?:\s*\(\s*\w{1,5}\s*\))*)?")
REF_RE = re.compile(rf"\b(?P<kind>{KIND})\s+(?P<list>{TOKEN}{CHAIN}(?:{JOIN}{TOKEN}{CHAIN})*)(?P<tail>{TAIL})", re.I)

LEVEL_OF = {"section": "UNIT", "rule": "UNIT", "schedule": "SCHEDULE", "sub-section": "SUB", "sub-rule": "SUB",
            "clause": "CLAUSE", "sub-clause": "SUBCLAUSE", "paragraph": "ENTRY", "para": "ENTRY", "item": "ITEM",
            "entry": "ENTRY", "proviso": "PROVISO", "explanation": "EXPLANATION"}
ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5"}


def _singular(kind: str) -> str:
    k = kind.lower()
    if k == "entries":
        return "entry"
    return k[:-1] if k.endswith("s") and k not in ("paras",) else ("para" if k == "paras" else k)


def _tokens(text: str) -> List[str]:
    return [t.strip("() ").strip() for t in re.findall(r"\(\s*\w{1,5}\s*\)|\d{1,3}[A-Z]{0,2}|[IVX]{1,4}", text)]


def _expand(items: List[Tuple[str, str]]) -> List[str]:
    """[('1',''),('3','to')] -> 1,2,3 ; [('a',''),('d','to')] -> a,b,c,d"""
    out: List[str] = []
    for value, joiner in items:
        if joiner == "to" and out:
            start = out[-1]
            if start.isdigit() and value.isdigit():
                out.extend(str(i) for i in range(int(start) + 1, int(value) + 1))
                continue
            if len(start) == 1 and len(value) == 1 and start.isalpha() and value.isalpha() and start < value:
                out.extend(chr(c) for c in range(ord(start) + 1, ord(value) + 1))
                continue
        out.append(value)
    return out


def split_list(list_text: str) -> List[List[str]]:
    """'(1), (1A) and (2)' -> [['1'],['1A'],['2']];  '16(2)(c)' -> [['16','2','c']];  '73 to 76' -> 73..76"""
    parts = re.split(r"\s*(,|\band\b|\bor\b|\bto\b|&)\s*", list_text)
    items: List[Tuple[List[str], str]] = []
    joiner = ""
    for part in parts:
        if part in (",", "and", "or", "to", "&"):
            joiner = "to" if part == "to" else ""
            continue
        toks = _tokens(part)
        if toks:
            items.append((toks, joiner))
            joiner = ""
    out: List[List[str]] = []
    for toks, joiner in items:
        if joiner == "to" and out and len(toks) == 1 and len(out[-1]) == 1:
            for v in _expand([(out[-1][0], ""), (toks[0], "to")])[1:]:
                out.append([v])
        else:
            out.append(toks)
    return out


def parse_tail(tail: str) -> Tuple[Optional[Tuple[str, str]], List[Tuple[str, str]]]:
    """' of sub-section (2) of section 16' -> (('section','16'), [('sub-section','2')]) (outer first)"""
    unit = None
    m = re.search(r"of\s+(section|rule|Schedule)\s+(\d{1,3}[A-Z]{0,2}|[IVX]{1,4})((?:\s*\(\s*\w{1,5}\s*\))*)\s*$", tail, re.I)
    inner: List[Tuple[str, str]] = []
    if m:
        unit = (m.group(1).lower(), m.group(2))
        inner = [("chain", t) for t in _tokens(m.group(3))]
        tail = tail[:m.start()]
    mids = re.findall(r"of\s+(?:the\s+)?(first\s+|second\s+|third\s+)?(sub-section|sub-rule|clause|sub-clause|paragraph|para|"
                      r"proviso|Explanation)\s*(\(\s*\w{1,5}\s*\)|\d{0,2})", tail, re.I)
    chain = [(kind.lower(), (ORDINALS.get(ordn.strip().lower(), "") if ordn else "") or tok.strip("() ") or "1")
             for ordn, kind, tok in mids]
    return unit, inner + list(reversed(chain))


# ---------------------------------------------------------------- tree walk

LEVELS_FOR = {
    "sub-section": {"SUB_SECTION", "SUB_RULE"}, "sub-rule": {"SUB_RULE", "SUB_SECTION"},
    "clause": {"CLAUSE"}, "sub-clause": {"SUB_CLAUSE", "ITEM"}, "item": {"ITEM", "SUB_ITEM"},
    "paragraph": {"ENTRY", "PARAGRAPH", "SUB_PARAGRAPH"}, "para": {"ENTRY", "PARAGRAPH", "SUB_PARAGRAPH"},
    "entry": {"ENTRY"}, "proviso": {"PROVISO"}, "explanation": {"EXPLANATION"},
}
NUMBERED = {"SUB_SECTION", "SUB_RULE", "CLAUSE", "SUB_CLAUSE", "ITEM", "SUB_ITEM", "ENTRY", "PARAGRAPH",
            "SUB_PARAGRAPH", "TABLE_ROW", "PROVISO", "EXPLANATION"}


def find_child(nodes: Dict[str, dict], children: Dict[str, List[str]], parent: str, token: str,
               kind: str = "") -> Optional[str]:
    """Child (or grandchild through a closing / table / proviso) of `parent` with printed number `token`
    and a level that fits `kind` ('sub-section', 'clause', ...; 'chain' = any numbered list level)."""
    token = token.strip("() ")
    allowed = LEVELS_FOR.get(kind, NUMBERED - {"PROVISO", "EXPLANATION"})
    frontier = [parent]
    for _depth in range(2):
        nxt = []
        for p in frontier:
            for c in children.get(p, []):
                n = nodes[c]
                if n["level"] in allowed and str(n.get("number", "")).strip("() ").lower() == token.lower():
                    return c
                if n["level"] in ("CLOSING", "TABLE", "PROVISO", "EXPLANATION"):
                    nxt.append(c)
        frontier = nxt
    return None


def walk(nodes: Dict[str, dict], children: Dict[str, List[str]], start: str, steps: Iterable[Tuple[str, str]]) -> Tuple[str, bool]:
    """Follow printed numbers from `start`; returns (deepest key reached, fully_resolved)."""
    current = start
    for kind, token in steps:
        nxt = find_child(nodes, children, current, token, kind)
        if nxt is None and kind != "chain":
            # statutes are loose with names ('clause (d)' for a sub-clause of a definition)
            nxt = find_child(nodes, children, current, token, "chain")
        if nxt is None:
            return current, False
        current = nxt
    return current, True


def convention_key(doc: str, unit_kind: str, unit_no: str, chain: List[str]) -> str:
    """Key by naming convention (used for references into another document that is not loaded)."""
    if unit_kind == "schedule":
        key = f"{doc}_SCH{unit_no}"
        prefixes = ["EN", "C", "SC", "I"]
    else:
        key = f"{doc}_{'R' if unit_kind == 'rule' else 'S'}{unit_no}"
        prefixes = ["SR" if unit_kind == "rule" else "SS", "C", "SC", "I"]
    for i, tok in enumerate(chain):
        key += f"_{prefixes[min(i, len(prefixes) - 1)]}{tok}"
    return key


CITATION_RE = re.compile(r"^\s*(?:(?P<kind>section|sec\.?|s\.|rule|r\.|schedule|sch\.?)\s*)?(?P<unit>\d{1,3}[A-Z]{0,2}|[IVX]{1,4})"
                         r"\s*(?:,?\s*(?:para(?:graph)?|entry)\s*(?P<para>\d{1,3}[A-Z]?))?(?P<chain>(?:\s*\(\s*\w{1,5}\s*\))*)\s*$", re.I)


def parse_citation(text: str) -> Optional[Tuple[str, str, List[str]]]:
    """'section 16(2)(c)' -> ('section','16',['2','c']); 'rule 36(4)' -> ('rule','36',['4']);
       'Schedule II para 5(b)' -> ('schedule','II',['5','b'])"""
    m = CITATION_RE.match(text)
    if not m:
        return None
    kind = (m.group("kind") or "section").lower().rstrip(".")
    kind = {"sec": "section", "s": "section", "r": "rule", "sch": "schedule"}.get(kind, kind)
    chain = ([m.group("para")] if m.group("para") else []) + _tokens(m.group("chain") or "")
    return kind, m.group("unit"), chain
