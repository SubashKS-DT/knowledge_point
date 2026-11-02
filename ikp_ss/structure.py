"""IKP / Step 2 - deterministic legal-structure parser.

Input : LayoutDocument (clean body lines in reading order).
Output: ParsedDocument with one Node per printed legal unit and a single clean
        text stream. Every node stores character ranges into that stream, so later
        passes LOOK UP text by node key and never search the PDF again.

Hierarchy handled
    FRONT / CHAPTER / SCHEDULE / ANNEX
      SECTION | RULE | ENTRY | PARAGRAPH
        SUB_SECTION | SUB_RULE (1) (1A)          -> SS / SR
          CLAUSE (a) (aa) (za)                   -> C
            SUB_CLAUSE (i) (ii)                  -> SC
              ITEM (A) (B)                       -> I
        PROVISO / EXPLANATION / ILLUSTRATION / CLOSING (wrap-up text after a list)
        HISTORICAL (older printed versions, e.g. CGST Rule 138 'NOTE: older versions')

Key format is the same as the old index: CGSTA17_S7_SS1_Ca, CGSTR17_R55_SR1_Cd,
CGSTA17_SCHII_EN5_Cb ... plus _P1 (proviso), _E1 (explanation), _CL1 (closing),
_SCi (sub-clause), _HIST1 (historical version), _ANX1 (annexure).
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .layout import LayoutDocument, Line

ROMAN_RE = re.compile(r"^(?=[ivxlc])(c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
ROMAN_VAL = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def roman_value(text: str) -> int:
    total = prev = 0
    for ch in reversed(text.lower()):
        v = ROMAN_VAL.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


def is_roman(text: str) -> bool:
    return bool(text) and bool(ROMAN_RE.match(text.lower()))


def int_to_roman(n: int) -> str:
    out = ""
    for v, s in ((100, "c"), (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += s
            n -= v
    return out


def num_key(text: str) -> Tuple[int, str]:
    m = re.match(r"^(\d+)([A-Za-z]*)$", text)
    return (int(m.group(1)), m.group(2).upper()) if m else (0, text)


# ------------------------------------------------------------------ nodes

@dataclass
class Node:
    key: str
    level: str
    number: str = ""
    parent: str = ""
    kind: str = ""  # marker family: NUM ALPHA ROMAN UPPER SECTION ...
    heading: str = ""
    marker_x: float = 0.0
    ranges: List[List[int]] = field(default_factory=list)  # own text [start, end) in stream
    children: List[str] = field(default_factory=list)
    order: int = 0
    bracketed: bool = False
    flags: List[str] = field(default_factory=list)
    footnotes: List[str] = field(default_factory=list)
    status: str = "ACTIVE"
    role: str = ""
    start: int = -1
    end: int = -1


@dataclass
class ParsedDocument:
    code: str
    doc_type: str
    stream: str
    nodes: Dict[str, Node]
    order: List[str]
    line_offsets: List[int]
    line_pages: List[int]
    marker_offsets: List[Tuple[int, str, int]]  # (stream offset, footnote number, page)
    events: List[str]
    seg_line: List[int] = field(default_factory=list)       # layout line index of each stream segment
    seg_line_off: List[int] = field(default_factory=list)   # char offset of the segment inside that line
    issue_keys: List[Tuple[str, int, str]] = field(default_factory=list)

    def page_of(self, offset: int) -> int:
        i = bisect.bisect_right(self.line_offsets, offset) - 1
        return self.line_pages[max(0, i)]

    def pages_of(self, start: int, end: int) -> List[int]:
        lo = bisect.bisect_right(self.line_offsets, start) - 1
        hi = bisect.bisect_left(self.line_offsets, end)
        return sorted(set(self.line_pages[max(0, lo):max(lo + 1, hi)]))

    def own_text(self, key: str) -> str:
        node = self.nodes[key]
        parts = [self.stream[a:b].strip() for a, b in node.ranges if self.stream[a:b].strip()]
        return "\n".join(parts)

    def full_text(self, key: str) -> str:
        node = self.nodes[key]
        return self.stream[node.start:node.end].strip() if node.start >= 0 else ""


# ------------------------------------------------------------------ patterns

LEAD = r"^[\[\s]*"
CHAPTER_RE = re.compile(r"^\[?\s*CHAPTER\s+([IVXLC]+)\b\.?\s*[-—–:]*\s*(.*)$")
PART_RE = re.compile(r"^\[?\s*PART\s+([IVXLC]+|[A-Z])\s*[-—–:]*\s*(.*)$")
SCHEDULE_RE = re.compile(r"^\[?\s*(?:THE\s+)?(?:(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH)\s+)?SCHEDULE"
                         r"(?:\s+([IVXLC]+|\d+))?\s*\]?\s*$")
ANNEX_TOP_RE = re.compile(r"^(REMOVAL OF DIFFICULTY ORDERS?|APPENDIX(?:\s+[IVXLC\d]+)?|FORMS?)\s*$")
ANNEXURE_RE = re.compile(r"^\[?\s*(ANNEXURE|ANNEX)(?:\s*[-–—]?\s*([IVXLCA-Z\d]+))?\s*\]?\s*$")
NOTE_RE = re.compile(r"^\[?\s*NOTE\s*[:.\-–—]")
SECTION_RE = re.compile(LEAD + r"(?:Rule\s+|Section\s+)?(\d{1,3}[A-Z]{0,3})\s*\.(?!\d)\s*(.*)$")
SECTION_NODOT_RE = re.compile(LEAD + r"(?:Rule\s+|Section\s+)?(\d{1,3}[A-Z]{0,3})\s+([A-Z][a-z].*)$")  # '[ 21A Suspension of ...' (bold only)
SUBPARA_RE = re.compile(LEAD + r"(\d{1,2})\.(\d{1,2})\.?\s+(.*)$")
MARKER_RE = re.compile(LEAD + r"\(\s*([0-9]{1,3}[A-Z]{0,2}|[a-z]{1,7}|[A-Z]{1,2})\s*\)\s*(.*)$")
PROVISO_RE = re.compile(LEAD + r"Provided\s+(?:further\s+|also\s+|always\s+|further\s+also\s+)?(?:that|always)\b", re.I)
EXPLANATION_RE = re.compile(LEAD + r"Explanation\s*(\d+|[IVX]+)?\s*(?:\[\s*(\d+)\s*\])?\s*[.:—–\-]", re.I)
ILLUSTRATION_RE = re.compile(LEAD + r"Illustrations?\b\s*[.:—–\-]?", re.I)
SUBJECT_RE = re.compile(r"^\s*(Subject|Sub)\s*[:.\-–]", re.I)
# Where the operative text of a notification / order begins when it has no "Subject:" line and no "1." paragraph:
# the standard enacting formula, optionally after the gazette reference ("G.S.R......(E).- In exercise of ...").
BODY_START_RE = re.compile(
    r"^\s*(?:(?:G\.?\s?S\.?\s?R\.?|S\.?\s?O\.?).{0,40}?[-–—:]\s*)?"
    r"(In\s+exercise\s+of\s+the\s+powers|WHEREAS\b|Whereas\b|In\s+pursuance\s+of\b|"
    r"The\s+(?:Central|State)\s+Government\b.{0,80}\bhereby\b)")
# text that follows a marker but means the marker was really a reference: "(1) of section 16"
REFERENCE_AFTER = re.compile(r"^\s*(of|to|in)\s+(this|that|the said|section|sub-section|rule|sub-rule|clause|sub-clause)\b|^\s*(and|or)\s+\(|^\s*read with\b")
# previous line ends with a word that expects a reference ('... under sub-section' + '(2) of section 9').
# '; or' / ', and' end a list item, so a marker after them is a real list item.
REFERENCE_BEFORE = re.compile(r"(?i)(?:^|[^;,:.\-—–\s])\s*\b(sub-section|section|sub-rule|rule|clause|sub-clause|item|"
                              r"paragraph|under|of|in|and|or|with|to|entry|serial|number|no\.)\s*\[?$")
HEADING_DELIM = re.compile(r"(\.\s*[-—–―]+|\s[-—–]{1,2}\s|[—–]{1,2}|:-|\.\s*:)")


def alpha_successor_ok(prev: str, cur: str) -> bool:
    """(c)->(d), (c)->(ca), (ca)->(cb), (z)->(za), (za)->(zb), (h)->(i) ... gap of 2 tolerated."""
    if cur <= prev:
        return False
    if cur.startswith(prev) and len(cur) == len(prev) + 1 and not (is_roman(prev) and is_roman(cur)):
        return True  # inserted (ca) after (c)  - but (i)->(ii) is roman, not an inserted clause
    if len(cur) == len(prev):
        if cur[:-1] == prev[:-1] and 0 < ord(cur[-1]) - ord(prev[-1]) <= 3:
            return True
        if len(cur) == 2 and cur[0] == cur[1] and len(set(prev)) == 1:  # (aa) (bb) style
            return True
    if len(cur) == len(prev) - 1 and 0 < ord(cur[-1]) - ord(prev[0]) <= 3:
        return True  # (ca) -> (d)
    if prev[0] == "z" and cur.startswith("z") and cur > prev:
        return True
    return False


def num_successor_ok(prev: str, cur: str) -> bool:
    a, b = num_key(prev), num_key(cur)
    if b <= a:
        return False
    if b[0] == a[0]:
        return True  # (1) -> (1A) -> (1B)
    return 0 < b[0] - a[0] <= 3


def roman_successor_ok(prev: str, cur: str) -> bool:
    p, c = roman_value(re.sub(r"[a-z]$", "", prev) if not is_roman(prev) else prev), None
    if is_roman(cur):
        c = roman_value(cur)
        return 0 < c - p <= 3
    base = re.match(r"^([ivxlc]+)([a-z])$", cur)
    return bool(base) and is_roman(base.group(1)) and roman_value(base.group(1)) in (p, p + 1)


LIST_KINDS = ("NUM", "ALPHA", "ROMAN", "UPPER")
TABLE_START_RE = re.compile(r"^\s*\[?\s*(?:Table|TABLE)\b|^\s*\[?\s*(?:S\.?\s*No\.?|Sl\.|Sl\.?\s*No\.?|Sr\.?\s*No\.?)(?:\s|$)")
TABLE_HEADER_RE = re.compile(r"^\s*(\(\s*\d{1,2}[A-Z]?\s*\)\s*){2,}$")
ROW_RE = re.compile(r"^\s*\[?\s*(\d{1,4}[A-Z]?)\s*[.)]?\s+\S")
ORDER_RE = re.compile(r"^\s*Order\s+No\.?\s*([\w./\-]+(?:\s*[-–]\s*[A-Za-z ]+?)?)\s*$")


class Parser:
    def __init__(self, layout: LayoutDocument, code: str, doc_type: str):
        self.layout = layout
        self.code = code
        self.doc_type = doc_type.upper().rstrip("S") if doc_type.upper() not in ("RULES",) else "RULES"
        if self.doc_type in ("CIRCULAR", "NOTIFICATION", "ORDER", "INSTRUCTION", "JUDGEMENT", "JUDGMENT"):
            self.profile = "PARAGRAPH"
        else:
            self.profile = "STATUTE"
        self.unit_level = "RULE" if self.doc_type == "RULES" else "SECTION"
        self.unit_prefix = "R" if self.doc_type == "RULES" else "S"
        self.sub_level = "SUB_RULE" if self.doc_type == "RULES" else "SUB_SECTION"
        self.sub_prefix = "SR" if self.doc_type == "RULES" else "SS"
        self.stream_parts: List[str] = []
        self.stream_len = 0
        self.nodes: Dict[str, Node] = {}
        self.order: List[str] = []
        self.stack: List[Node] = []
        self.line_offsets: List[int] = []
        self.line_pages: List[int] = []
        self.marker_offsets: List[Tuple[int, str, int]] = []
        self.events: List[str] = []
        self.tails: Dict[str, str] = {}
        self.seg_line: List[int] = []
        self.seg_line_off: List[int] = []
        self.line_index = {id(l): i for i, l in enumerate(layout.lines)}
        self.issue_keys: List[Tuple[str, int, str]] = []  # (node key, page, issue) for per-chapter gates
        self.pending_heading: Optional[Tuple[Node, str, int]] = None  # (node, text so far, lines seen)
        self.last_chapter = 0
        self.last_unit: Optional[Tuple[int, str]] = None
        self.last_entry: Optional[Tuple[int, str]] = None
        self.mode = "FRONT"
        self.history_mode = False
        self.bold_headings = self._uses_bold_headings()
        self.max_x = max((l.x1 for l in layout.lines), default=600)
        xs = sorted(l.x0 for l in layout.lines) or [0]
        self.left_margin = xs[int(len(xs) * 0.05)]
        self.margin_by_page: Dict[int, list] = {}
        for note in layout.margin_notes:
            self.margin_by_page.setdefault(note.page, []).append(note)
        self.root = self._new(Node(code, "DOCUMENT", parent="", kind="ROOT"), attach=False)
        self.front = self._new(Node(f"{code}_FRONT", "FRONT", parent=code, kind="FRONT", heading="FRONT MATTER"))
        self.stack = [self.root, self.front]

    # ------------------------------------------------------------ helpers
    def _uses_bold_headings(self) -> bool:
        bold = total = 0
        for line in self.layout.lines:
            if SECTION_RE.match(line.text) and re.match(LEAD + r"\d{1,3}[A-Z]{0,3}\s*\.\s*[A-Z\[(]", line.text):
                total += 1
                bold += line.bold_start
        return total >= 5 and bold / total >= 0.25

    def _new(self, node: Node, attach: bool = True) -> Node:
        base, n = node.key, 2
        while node.key in self.nodes:
            node.key = f"{base}_{n}"
            n += 1
        if node.key != base:
            node.flags.append("DUPLICATE_KEY_SUFFIXED")
        node.order = len(self.order)
        self.nodes[node.key] = node
        self.order.append(node.key)
        if attach and node.parent in self.nodes:
            self.nodes[node.parent].children.append(node.key)
        return node

    def _append(self, node: Node, text: str, line: Line, new_block: bool, offset_in_line: int = 0) -> None:
        if not text:
            return
        if self.stream_len == 0:
            sep = ""
        elif new_block:
            sep = "\n"
        elif self.stream_parts and self.stream_parts[-1].endswith("-") and not self.stream_parts[-1].endswith(" -"):
            sep = ""
        else:
            sep = " "
        self.stream_parts.append(sep)
        self.stream_len += len(sep)
        start = self.stream_len
        self.line_offsets.append(start)
        self.line_pages.append(line.page)
        # where this stream segment came from in the PDF: layout line index + char offset in that line
        self.seg_line.append(self.line_index.get(id(line), -1))
        self.seg_line_off.append(offset_in_line)
        self.stream_parts.append(text)
        self.stream_len += len(text)
        self.tails[node.key] = text
        if node.ranges and node.ranges[-1][1] == start - len(sep):
            node.ranges[-1][1] = self.stream_len
        else:
            node.ranges.append([start, self.stream_len])
        for off, number in line.fn_markers:
            rel = off - offset_in_line
            if 0 <= rel <= len(text):
                self.marker_offsets.append((start + rel, number, line.page))

    def _pop_to(self, levels: Tuple[str, ...]) -> Node:
        while len(self.stack) > 1 and self.stack[-1].level not in levels:
            self.stack.pop()
        return self.stack[-1]

    def _unit_node(self) -> Optional[Node]:
        for node in reversed(self.stack):
            if node.level in (self.unit_level, "ENTRY", "PARAGRAPH", "HISTORICAL"):
                return node
        return None

    def _container(self) -> Node:
        for node in reversed(self.stack):
            if node.level in ("CHAPTER", "SCHEDULE", "PART", "ANNEX", "DOCUMENT", "FRONT"):
                return node
        return self.root

    def _list_level(self, kind: str, parent: Node) -> Tuple[str, str]:
        unit = parent.level in (self.unit_level, "HISTORICAL", "ENTRY", "PARAGRAPH")
        definitions = unit and re.search(r"(?i)definition|interpretation", parent.heading or "")
        if kind == "NUM":
            if unit and not definitions and parent.level != "ENTRY":
                return self.sub_level, self.sub_prefix
            return ("CLAUSE", "C") if (definitions or parent.level == "ENTRY") else ("SUB_ITEM", "N")
        if kind == "ALPHA":
            return ("SUB_CLAUSE", "SC") if parent.level == "CLAUSE" else ("CLAUSE", "C")
        if kind == "ROMAN":
            return ("ITEM", "I") if parent.level == "SUB_CLAUSE" else ("SUB_CLAUSE", "SC")
        return "ITEM", "I"

    # ------------------------------------------------------------ marker logic
    def _classify_marker(self, token: str) -> List[str]:
        if re.fullmatch(r"\d{1,3}[A-Z]{0,2}", token):
            return ["NUM"]
        if re.fullmatch(r"[A-Z]{1,2}", token):
            return ["UPPER"]
        kinds = []
        if is_roman(token) or re.fullmatch(r"(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)[a-z]", token):
            kinds.append("ROMAN")
        if re.fullmatch(r"[a-z]{1,3}", token) and not (len(token) > 1 and is_roman(token) and len(set(token)) > 1):
            kinds.append("ALPHA")
        return kinds

    def _open_of_kind(self, kind: str) -> Optional[Node]:
        for node in reversed(self.stack):
            if node.kind == kind:
                return node
            if node.level in (self.unit_level, "ENTRY", "PARAGRAPH", "HISTORICAL", "CHAPTER", "SCHEDULE"):
                return None
        return None

    def _find_sibling(self, kind: str, token: str, ok, x: Optional[float] = None) -> Optional[Node]:
        boundary = (self.unit_level, "ENTRY", "PARAGRAPH", "HISTORICAL", "CHAPTER", "SCHEDULE", "ANNEXURE")
        def aligned(n: Node) -> bool:
            # printed lists keep their indent; a 20pt+ difference means a different list level
            return x is None or n.marker_x == 0 or abs(n.marker_x - x) <= 20
        for node in reversed(self.stack):
            if node.kind == kind:
                return node if ok(node.number, token) and aligned(node) else None
            same = [self.nodes[c] for c in node.children if self.nodes[c].kind == kind]
            if same and ok(same[-1].number, token) and aligned(same[-1]):
                return same[-1]
            if node.level in boundary:
                return None
        return None

    def _resolve_marker(self, token: str, x: Optional[float] = None) -> Optional[Tuple[str, Optional[Node]]]:
        """Return (kind, sibling_or_None). None = not a structural marker (a reference)."""
        kinds = self._classify_marker(token)
        if not kinds:
            return None
        ok = {"NUM": num_successor_ok, "ALPHA": alpha_successor_ok, "ROMAN": roman_successor_ok,
              "UPPER": lambda p, c: alpha_successor_ok(p.lower(), c.lower())}
        starts = {"NUM": ("1",), "ALPHA": ("a",), "ROMAN": ("i",), "UPPER": ("A",)}
        # 1. continue a list: an open node of that kind, or the last child of that kind of any
        #    open node (covers lists interrupted by a proviso / explanation / closing text)
        for kind in kinds:
            sib = self._find_sibling(kind, token, ok[kind], x)
            if sib is not None:
                return kind, sib
        for kind in kinds:  # second chance without the indent check (layouts with flat indents)
            sib = self._find_sibling(kind, token, ok[kind])
            if sib is not None and not (token in ("i", "v", "x") and len(kinds) > 1):
                return kind, sib
        # 2. start a new list
        for kind in sorted(kinds, key=lambda k: 0 if k == "ROMAN" and token == "i" else 1):
            if token in starts[kind] or (kind == "NUM" and token in ("1A",)):
                sib = self._open_of_kind(kind)
                if sib is None or kind != "NUM" or self.stack[-1].kind in ("CLOSING", "PROVISO", "EXPLANATION", "ALPHA", "ROMAN"):
                    return kind, None
        return None

    # ------------------------------------------------------------ main loop
    def parse(self) -> ParsedDocument:
        lines = self.layout.lines
        i = 0
        while i < len(lines):
            consumed = self._handle_line(lines, i)
            i += consumed
        self._finish()
        return ParsedDocument(self.code, self.doc_type, "".join(self.stream_parts), self.nodes, self.order,
                              self.line_offsets, self.line_pages, self.marker_offsets, self.events,
                              self.seg_line, self.seg_line_off, self.issue_keys)

    def _handle_line(self, lines: List[Line], i: int) -> int:
        line = lines[i]
        text = line.text
        prev = lines[i - 1] if i > 0 else None

        # ---------------- top-level containers
        m = CHAPTER_RE.match(text)
        if m and self.profile == "STATUTE":
            n = roman_value(m.group(1))
            if n > self.last_chapter and not text.rstrip().endswith((",", ";")):
                return self._start_chapter(lines, i, m)
            self.events.append(f"p{line.page}: ignored repeated chapter line '{text[:50]}'")
            return 1
        m = SCHEDULE_RE.match(text)
        if m and self.profile == "STATUTE" and self.mode != "ANNEX":
            return self._start_schedule(lines, i, m)
        if ANNEX_TOP_RE.match(text) and self.mode in ("SCHEDULE", "BODY") and self.profile == "STATUTE":
            self._start_container("ANNEX", f"{self.code}_ANX", text, line, "ANNEX")
            return 1
        if self.mode == "ANNEX":
            return self._handle_annex_line(line, text)

        if self.profile == "PARAGRAPH":
            return self._handle_paragraph_doc(lines, i)

        # ---------------- annexure / note inside a unit
        unit = self._unit_node()
        if unit is not None and ANNEXURE_RE.match(text):
            node = self._new(Node(f"{unit.key}_ANX1", "ANNEXURE", parent=unit.key, kind="ANNEXURE",
                                  heading=text.strip("[] "), marker_x=line.x0))
            self.stack = self.stack[:self.stack.index(unit) + 1] + [node]
            self._append(node, text, line, True)
            return 1
        if unit is not None and NOTE_RE.match(text):
            node = self._new(Node(f"{unit.key}_NOTE1", "NOTE", parent=unit.key, kind="NOTE", marker_x=line.x0))
            self.stack = self.stack[:self.stack.index(unit) + 1] + [node]
            self._append(node, text, line, True)
            if re.search(r"(?i)older version|earlier version|as substituted|originally", text + " " +
                         " ".join(l.text for l in lines[i + 1:i + 3])):
                self.history_mode = True
            return 1

        # ---------------- section / rule / schedule entry
        m = SECTION_RE.match(text)
        if not m and line.bold_start and self.mode == "BODY":
            m = SECTION_NODOT_RE.match(text)
        if m and self._looks_like_unit_heading(line, m, prev):
            return self._start_unit(lines, i, m)

        # inside an annexure everything else is table text
        if self.stack[-1].level in ("ANNEXURE", "TABLE_ROW") and self._in_annexure():
            return self._table_line(line, text, inside_annexure=True)

        if self.mode == "FRONT" or self._unit_node() is None:
            self._append(self.stack[-1], text, line, line.para_start is not False)
            return 1

        return self._handle_body_line(line, text, prev, 0)

    # ------------------------------------------------------------ containers
    def _start_chapter(self, lines: List[Line], i: int, m) -> int:
        line = lines[i]
        roman = m.group(1)
        title = m.group(2).strip()
        consumed = 1
        if not title and i + 1 < len(lines):
            nxt = lines[i + 1].text
            letters = re.sub(r"[^A-Za-z]", "", nxt)
            if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7 and not SECTION_RE.match(nxt):
                title = nxt.strip()
                consumed = 2
        self.last_chapter = roman_value(roman)
        self.mode = "BODY"
        node = self._start_container("CHAPTER", f"{self.code}_CH{roman}", line.text, line,
                                     "CHAPTER", number=roman, heading=title)
        for extra in lines[i + 1:i + consumed]:
            self._append(node, extra.text, extra, False)
        return consumed

    def _start_schedule(self, lines: List[Line], i: int, m) -> int:
        line = lines[i]
        ordinal = {"FIRST": "I", "SECOND": "II", "THIRD": "III", "FOURTH": "IV", "FIFTH": "V", "SIXTH": "VI",
                   "SEVENTH": "VII"}
        number = (m.group(2) or ordinal.get(m.group(1) or "", "") or "I").upper()
        key = f"{self.code}_SCH{number}"
        if key in self.nodes:
            self.events.append(f"p{line.page}: ignored repeated schedule line '{line.text}'")
            return 1
        self.mode = "SCHEDULE"
        self.last_entry = None
        node = self._start_container("SCHEDULE", key, line.text, line, "SCHEDULE", number=number)
        consumed = 1
        title_parts = []
        # "[See section 7]" and the bold upper-case title belong to the schedule heading
        for nxt in lines[i + 1:i + 5]:
            t = nxt.text.strip()
            if re.match(r"^\[?\s*\(?\s*See\s", t, re.I) or (
                    re.sub(r"[^A-Za-z]", "", t).isupper() and not SECTION_RE.match(t) and not MARKER_RE.match(t)):
                self._append(node, t, nxt, False)
                title_parts.append(t)
                consumed += 1
            else:
                break
        node.heading = " ".join(title_parts)
        return consumed

    def _start_container(self, level: str, key: str, text: str, line: Line, kind: str,
                         number: str = "", heading: str = "") -> Node:
        if level == "ANNEX":
            self.mode = "ANNEX"
        node = self._new(Node(key, level, number=number, parent=self.code, kind=kind,
                              heading=heading or text, marker_x=line.x0))
        self.stack = [self.root, node]
        self._append(node, text, line, True)
        return node

    # ------------------------------------------------------------ units
    def _looks_like_unit_heading(self, line: Line, m, prev: Optional[Line]) -> bool:
        number, rest = m.group(1), m.group(2)
        if self.mode == "FRONT" and self.last_chapter == 0 and self.doc_type not in ("ACT", "RULES"):
            return False
        if self.mode == "SCHEDULE":
            if self.last_entry is not None and num_key(number) <= self.last_entry:
                return False
            if self.last_entry is not None and num_key(number)[0] - self.last_entry[0] > 3:
                return False
            if self.last_entry is None and num_key(number)[0] > 2:
                return False
            return not (rest[:1].islower() or re.match(r"^\s*\d{3,}", rest))
        if self.mode == "FRONT" and self.last_chapter == 0:
            # Acts/Rules without CHAPTER headings start at section 1
            if number != "1":
                return False
        if re.match(r"^\s*\d{3,}", rest):  # tariff row: '27. 0406 Chena or paneer'
            return False
        if number.startswith("0") or not rest.strip():  # '07.' / '9B.' fragments
            return False
        if re.match(r"^\s*\[?\s*[a-z]", rest):  # '11.[prepared on the basis ...'
            return False
        if rest and rest[0].islower():
            return False
        if self.bold_headings and not line.bold_start:
            return False
        if not self.bold_headings and line.para_start is False:
            return False
        if prev is not None and REFERENCE_BEFORE.search(prev.text) and line.para_start is False:
            return False
        if self.last_unit is not None and num_key(number) <= self.last_unit:
            return bool(line.bold_start)  # handled as historical/duplicate version
        if self.last_unit is not None and num_key(number)[0] - self.last_unit[0] > 25:
            return False
        return True

    def _start_unit(self, lines: List[Line], i: int, m) -> int:
        line = lines[i]
        number, rest = m.group(1).upper(), m.group(2)
        bracketed = line.text.lstrip().startswith("[")
        if self.mode == "SCHEDULE":
            sched = self._pop_to(("SCHEDULE",))
            self.last_entry = num_key(number)
            node = self._new(Node(f"{sched.key}_EN{number}", "ENTRY", number=number, parent=sched.key,
                                  kind="ENTRY", marker_x=line.x0, bracketed=bracketed))
            self.stack = self.stack[:self.stack.index(sched) + 1] + [node]
            return self._unit_body(line, node, m)
        if self.mode == "FRONT":
            self.mode = "BODY"
        key_number = num_key(number)
        if self.last_unit is not None and key_number <= self.last_unit:
            existing = f"{self.code}_{self.unit_prefix}{number}"
            parent = existing if existing in self.nodes else (self._unit_node().key if self._unit_node() else self.code)
            node = self._new(Node(f"{parent}_HIST1", "HISTORICAL", number=number, parent=parent, kind="HISTORICAL",
                                  marker_x=line.x0, bracketed=bracketed, status="HISTORICAL",
                                  flags=["OUT_OF_SEQUENCE_VERSION" if not self.history_mode else "HISTORICAL_VERSION"]))
            self.events.append(f"p{line.page}: {self.unit_level} {number} printed again after "
                               f"{self.last_unit}; stored as {node.key}")
            container = self._container()
            self.stack = self.stack[:self.stack.index(container) + 1] + [node]
            return self._unit_body(line, node, m)
        if self.last_unit is not None and key_number[0] - self.last_unit[0] > 3:
            self.events.append(f"p{line.page}: numbering jumps {self.last_unit} -> {number}")
        self.last_unit = key_number
        self.history_mode = False
        container = self._container()
        node = self._new(Node(f"{self.code}_{self.unit_prefix}{number}", self.unit_level, number=number,
                              parent=container.key, kind="SECTION", marker_x=line.x0, bracketed=bracketed))
        self.stack = self.stack[:self.stack.index(container) + 1] + [node]
        return self._unit_body(line, node, m)

    def _unit_body(self, line: Line, node: Node, m) -> int:
        text = line.text
        rest_start = m.start(2)
        rest = text[rest_start:]
        heading = ""
        # Gazette style: heading printed in the margin at the same height
        note = self._margin_heading(line)
        if note:
            heading = note
        # Inline first sub-unit:  '7. Scope of supply.— (1) For the ...'  /  '5. (1) Subject to'
        split_at = None
        dm = HEADING_DELIM.search(rest[:260]) if not note else None
        if dm and node.level != "ENTRY":
            heading = heading or rest[:dm.start()].strip(" .[")
            after = rest[dm.end():]
            mm = MARKER_RE.match(after)
            if mm and mm.group(1) in ("1", "a", "i"):
                split_at = rest_start + dm.end() + (len(after) - len(after.lstrip()))
        else:
            mm = MARKER_RE.match(rest)
            if mm and mm.group(1) in ("1", "a"):
                split_at = rest_start + (len(rest) - len(rest.lstrip()))
        if node.level == "ENTRY" and split_at is None:
            heading = re.split(r"[.—–:]\s|—|––", rest, maxsplit=1)[0].strip(" .[")[:150]
        node.heading = heading.strip()
        self.pending_heading = None
        if not dm and not note and node.level != "ENTRY" and split_at is None and node.kind in ("SECTION", "HISTORICAL") \
                and self.profile == "STATUTE":
            self.pending_heading = (node, rest, 1)
        if split_at is not None:
            self._append(node, text[:split_at].rstrip(), line, True)
            return self._handle_body_line(line, text[split_at:], None, split_at, new_block=False) or 1
        self._append(node, text, line, True)
        return 1

    def _margin_heading(self, line: Line) -> str:
        for note in self.margin_by_page.get(line.page, []):
            if abs(note.y0 - line.y0) <= 8 and not re.fullmatch(r"[\d\s]+of\s+\d{4}\.?", note.text) \
                    and re.search(r"[A-Za-z]{3}", note.text):
                return note.text.rstrip(".")
        return ""

    # ------------------------------------------------------------ inside a unit
    def _handle_body_line(self, line: Line, text: str, prev: Optional[Line], offset: int,
                          new_block: Optional[bool] = None) -> int:
        top = self.stack[-1]
        nb = line.para_start is not False if new_block is None else new_block

        # heading that wraps onto the next line(s):  '6. Authorisation of ... as proper' / 'officer ....— (1) The'
        if self.pending_heading and offset == 0:
            node, so_far, seen = self.pending_heading
            if top is node and line.para_start is False and not MARKER_RE.match(text):
                dm = HEADING_DELIM.search(text[:200])
                if dm:
                    node.heading = (so_far + " " + text[:dm.start()]).strip(" .[")
                    self.pending_heading = None
                    after = text[dm.end():]
                    mm = MARKER_RE.match(after)
                    if mm and mm.group(1) in ("1", "a", "i"):
                        cut = dm.end() + (len(after) - len(after.lstrip()))
                        self._append(node, text[:cut].rstrip(), line, False)
                        return self._handle_body_line(line, text[cut:], None, cut, new_block=False)
                    self._append(node, text, line, False)
                    return 1
                if seen < 3:
                    self.pending_heading = (node, so_far + " " + text, seen + 1)
                    self._append(node, text, line, False)
                    return 1
            self.pending_heading = None

        # ---- tables inside a provision: 'Table' / 'S. No.' / '(1) (2) (3)' start one
        if offset == 0 and top.level in ("TABLE", "TABLE_ROW"):
            mm0 = MARKER_RE.match(text)
            leaves = bool(PROVISO_RE.match(text) or EXPLANATION_RE.match(text))
            if mm0 and not TABLE_HEADER_RE.match(text):
                res = self._resolve_marker(mm0.group(1), line.x0)
                leaves = leaves or bool(res and res[1] is not None)
            if not leaves:
                return self._table_line(line, text)
            table = top if top.level == "TABLE" else self.nodes[top.parent]
            self.stack = self.stack[:self.stack.index(table)]
            top = self.stack[-1]
        if offset == 0 and (TABLE_START_RE.match(text) or TABLE_HEADER_RE.match(text)) and self._unit_node() is not None:
            count = sum(1 for k in top.children if self.nodes[k].level == "TABLE") + 1
            node = self._new(Node(f"{top.key}_TBL{count}", "TABLE", number=str(count), parent=top.key,
                                  kind="TABLE", marker_x=line.x0))
            self.stack.append(node)
            self._append(node, text, line, True)
            return 1

        if PROVISO_RE.match(text):
            parent = self._attach_point(line, text, "PROVISO")
            count = sum(1 for k in parent.children if self.nodes[k].level == "PROVISO") + 1
            node = self._new(Node(f"{parent.key}_P{count}", "PROVISO", number=str(count), parent=parent.key,
                                  kind="PROVISO", marker_x=line.x0, bracketed=text.lstrip().startswith("[")))
            self.stack = self.stack[:self.stack.index(parent) + 1] + [node]
            self._append(node, text, line, True, offset)
            return 1
        em = EXPLANATION_RE.match(text)
        if em:
            parent = self._attach_point(line, text, "EXPLANATION")
            count = sum(1 for k in parent.children if self.nodes[k].level == "EXPLANATION") + 1
            label = em.group(1) or str(count)
            node = self._new(Node(f"{parent.key}_E{label}", "EXPLANATION", number=label, parent=parent.key,
                                  kind="EXPLANATION", marker_x=line.x0, bracketed=text.lstrip().startswith("[")))
            self.stack = self.stack[:self.stack.index(parent) + 1] + [node]
            self._append(node, text, line, True, offset)
            return 1
        if ILLUSTRATION_RE.match(text) and nb:
            parent = self._attach_point(line, text, "ILLUSTRATION")
            count = sum(1 for k in parent.children if self.nodes[k].level == "ILLUSTRATION") + 1
            node = self._new(Node(f"{parent.key}_ILL{count}", "ILLUSTRATION", number=str(count), parent=parent.key,
                                  kind="ILLUSTRATION", marker_x=line.x0))
            self.stack = self.stack[:self.stack.index(parent) + 1] + [node]
            self._append(node, text, line, True, offset)
            return 1

        mm = MARKER_RE.match(text)
        if mm and (mm.group(2) or "").strip() and re.fullmatch(r"(\(\s*\w{1,4}\s*\)\s*)+", mm.group(2)):
            mm = None  # table column header '(1) (2) (3)'
        if mm and re.match(r"^\s*[,;:.)\]]", text[mm.end(1):].split(")", 1)[-1] if ")" in text[mm.end(1):] else ""):
            mm = None  # '(i), vide number G.S.R ...' - a reference that wrapped onto a new line
        if mm and not (line.para_start is not True and REFERENCE_AFTER.match(mm.group(2) or "")) and not (
                prev is not None and REFERENCE_BEFORE.search(prev.text) and line.para_start is False):
            resolved = self._resolve_marker(mm.group(1), line.x0 if offset == 0 else None)
            if resolved:
                kind, sibling = resolved
                if sibling is not None:
                    parent = self.nodes[sibling.parent]
                    self.stack = self.stack[:self.stack.index(parent) + 1]
                else:
                    parent = self.stack[-1]
                    if parent.level in ("PROVISO", "EXPLANATION", "ILLUSTRATION", "CLOSING", "NOTE"):
                        pass  # a list that belongs to the proviso / explanation / closing text
                level, prefix = self._list_level(kind, parent)
                token = mm.group(1)
                node = self._new(Node(f"{parent.key}_{prefix}{token}", level, number=token, parent=parent.key,
                                      kind=kind, marker_x=line.x0 if offset == 0 else self.stack[-1].marker_x,
                                      bracketed=text.lstrip().startswith("[")))
                self.stack.append(node)
                rest = mm.group(2) or ""
                inner = MARKER_RE.match(rest)
                cut = mm.start(2)
                if not inner:  # '(4) Registration for the examination and payment of fee.-(i)A person'
                    dm = HEADING_DELIM.search(rest[:160])
                    if dm:
                        after = rest[dm.end():]
                        inner = MARKER_RE.match(after)
                        cut = mm.start(2) + dm.end() + (len(after) - len(after.lstrip()))
                        if inner and inner.group(1) in ("a", "i", "A", "1"):
                            node.heading = rest[:dm.start()].strip(" .")
                if inner and inner.group(1) in ("a", "i", "A", "1") and rest is not text:
                    self._append(node, text[:cut].rstrip(), line, True, offset)
                    return self._handle_body_line(line, text[cut:], None, offset + cut, new_block=False)
                self._append(node, text, line, True, offset)
                return 1
            if line.para_start:
                self.issue_keys.append((top.key, line.page, f"marker ({mm.group(1)}) out of sequence"))
                self.events.append(f"p{line.page}: marker '({mm.group(1)})' out of sequence under {top.key}; "
                                   f"kept as text")
                top.flags.append("MARKER_OUT_OF_SEQUENCE")

        # wrap-up text after a list:  '(d) ... Board,\n the consigner may issue ...'
        parent_x = self.nodes[top.parent].marker_x if top.parent in self.nodes else -99
        at_item = abs(line.x0 - top.marker_x) <= 4
        at_parent = abs(line.x0 - parent_x) <= 4 and parent_x > self.left_margin + 10 and text[:1].islower()
        prev_text = self.own_tail(top)
        item_done = bool(re.search(r"([,;]|\bor|\band|––|—)\s*$", prev_text)) or (
            self.profile == "PARAGRAPH" and bool(re.search(r"[,;:)]\s*[-–—]+\s*$", prev_text)))
        # Gazette layout prints wrap-up text full width (left margin) after an item ending in ','
        at_left = abs(line.x0 - self.left_margin) <= 4 and text[:1].islower() and item_done
        # closing text starts lower-case ('shall be treated ...', 'the consigner may ...') after a finished item
        # notifications run on without a paragraph gap: wrap-up text shows only by moving back left of the item
        at_outdent = (self.profile == "PARAGRAPH" and text[:1].islower() and item_done and
                      (line.x0 < top.marker_x - 12 or (prev is not None and line.x0 < prev.x0 - 10)))
        if (line.para_start or at_outdent) and top.kind in LIST_KINDS and offset == 0 and (
                at_item or at_parent or at_left or at_outdent):
            if text[:1].islower() and item_done:
                parent = self.nodes[top.parent]
                count = sum(1 for k in parent.children if self.nodes[k].level == "CLOSING") + 1
                node = self._new(Node(f"{parent.key}_CL{count}", "CLOSING", number=str(count), parent=parent.key,
                                      kind="CLOSING", marker_x=line.x0))
                self.stack = self.stack[:self.stack.index(parent) + 1] + [node]
                self._append(node, text, line, True, offset)
                return 1
        self._append(top, text, line, nb if offset == 0 else False, offset)
        return 1

    def own_tail(self, node: Node) -> str:
        return self.tails.get(node.key, "")

    def _attach_point(self, line: Line, text: str, what: str) -> Node:
        """Where does a proviso / explanation belong?"""
        unit = self._unit_node()
        # explicit scope words win: 'For the purposes of this section' -> the section
        scope = re.search(r"(?i)\bthis\s+(section|rule|sub-section|sub-rule|clause|sub-clause|paragraph|entry|"
                          r"chapter|act|schedule)\b", text[:200])
        if scope and what == "EXPLANATION":
            word = scope.group(1).lower()
            wanted = {"section": (self.unit_level, "HISTORICAL"), "rule": (self.unit_level, "HISTORICAL"),
                      "sub-section": (self.sub_level,), "sub-rule": (self.sub_level,), "clause": ("CLAUSE",),
                      "sub-clause": ("SUB_CLAUSE",), "paragraph": ("ENTRY", "PARAGRAPH"), "entry": ("ENTRY",)}.get(word)
            if wanted:
                for node in reversed(self.stack):
                    if node.level in wanted:
                        return node
            if word in ("chapter", "act", "schedule") and unit is not None:
                return unit
        top = self.stack[-1]
        # a further proviso is a sibling of the previous proviso
        while top.level in ("PROVISO", "EXPLANATION", "ILLUSTRATION", "CLOSING") and len(self.stack) > 2:
            if what == "PROVISO" and top.level == "EXPLANATION":
                break
            self.stack.pop()
            top = self.stack[-1]
        # gazette style: proviso printed at the sub-section indent belongs to the sub-section
        if top.kind in LIST_KINDS:
            for node in reversed(self.stack):
                if node.kind in LIST_KINDS and node.marker_x <= line.x0 + 3:
                    if node is not top:
                        return node
                    break
            if top.kind != "NUM":
                top.flags.append(f"{what}_ATTACH_REVIEW")
        return top

    # ------------------------------------------------------------ tables / annexures / annex orders
    def _in_annexure(self) -> bool:
        return any(n.level == "ANNEXURE" for n in self.stack)

    def _table_line(self, line: Line, text: str, inside_annexure: bool = False) -> int:
        """Rows start with the next serial number ('1.', '2', '453.'); other lines continue the row."""
        top = self.stack[-1]
        table = top if top.level in ("TABLE", "ANNEXURE") else self.nodes[top.parent]
        rows = [self.nodes[k] for k in table.children if self.nodes[k].level == "TABLE_ROW"]
        m = ROW_RE.match(text)
        if m and not TABLE_HEADER_RE.match(text):
            number = m.group(1)
            expected = num_key(rows[-1].number)[0] + 1 if rows else 1
            if num_key(number)[0] == expected or (not rows and num_key(number)[0] <= 2):
                node = self._new(Node(f"{table.key}_ROW{number}", "TABLE_ROW", number=number, parent=table.key,
                                      kind="TABLE_ROW", marker_x=line.x0))
                self.stack = self.stack[:self.stack.index(table) + 1] + [node]
                self._append(node, text, line, True)
                return 1
        target = top if top.level == "TABLE_ROW" else table
        self._append(target, text, line, target.level == "TABLE" and line.para_start is not False)
        return 1

    def _handle_annex_line(self, line: Line, text: str) -> int:
        """Removal of Difficulty Orders etc.: ANNEX > ORDER ('Order No. 01/2017-Central Tax') > PARAGRAPH."""
        annex = next((n for n in self.stack if n.level == "ANNEX"), self.stack[-1])
        om = ORDER_RE.match(text)
        if om and "dated" not in text.lower():
            count = sum(1 for k in annex.children if self.nodes[k].level == "ORDER") + 1
            node = self._new(Node(f"{annex.key}_O{count}", "ORDER", number=str(count), parent=annex.key,
                                  kind="ORDER", heading=text.strip(), marker_x=line.x0))
            self.stack = self.stack[:self.stack.index(annex) + 1] + [node]
            self._append(node, text, line, True)
            return 1
        order = next((n for n in reversed(self.stack) if n.level == "ORDER"), None)
        m = SECTION_RE.match(text)
        if order is not None and m:
            paras = [self.nodes[k] for k in order.children if self.nodes[k].level == "PARAGRAPH"]
            expected = num_key(paras[-1].number)[0] + 1 if paras else 1
            if num_key(m.group(1))[0] == expected:
                node = self._new(Node(f"{order.key}_PA{m.group(1)}", "PARAGRAPH", number=m.group(1),
                                      parent=order.key, kind="ANNEX_PARA", marker_x=line.x0,
                                      heading=re.split(r"[.—–:]\s*[-—–]+|––|—", m.group(2), maxsplit=1)[0].strip(" .")[:120]
                                      if re.search(r"[—–]|\.-", m.group(2)[:80]) else ""))
                self.stack = self.stack[:self.stack.index(order) + 1] + [node]
                self._append(node, text, line, True)
                return 1
        self._append(self.stack[-1], text, line, line.para_start is not False)
        return 1

    # ------------------------------------------------------------ circulars / notifications
    def _handle_paragraph_doc(self, lines: List[Line], i: int) -> int:
        line = lines[i]
        text = line.text
        top = self.stack[-1]
        if self.mode == "FRONT":
            if SUBJECT_RE.match(text):
                node = self._new(Node(f"{self.code}_SUBJECT", "SUBJECT", parent=self.code, kind="SUBJECT",
                                      heading=re.sub(SUBJECT_RE, "", text).strip(), marker_x=line.x0))
                self.stack = [self.root, node]
                self.mode = "SUBJECT"
                self._append(node, text, line, True)
                return 1
            m = SECTION_RE.match(text)
            if not (m and m.group(1) == "1"):
                if BODY_START_RE.match(text):
                    # Notification / order without "Subject:" or "1.": the enacting formula opens paragraph 1;
                    # its (a), (b), (i) ... become clauses, a later "2." the next paragraph.
                    self.mode = "BODY"
                    self.last_unit = (1, "")
                    node = self._new(Node(f"{self.code}_PA1", "PARAGRAPH", number="1", parent=self.code,
                                          kind="SECTION", marker_x=line.x0, flags=["IMPLICIT_NUMBER"]))
                    self.stack = [self.root, node]
                    self._append(node, text, line, True)
                    return 1
                self._append(top, text, line, line.para_start is not False)
                return 1
        if self.mode == "SUBJECT":
            if line.para_start is False:
                top.heading = (top.heading + " " + text).strip()
                self._append(top, text, line, False)
                return 1
            self.mode = "BODY"
            if not SECTION_RE.match(text):
                self.last_unit = (1, "")
                node = self._new(Node(f"{self.code}_PA1", "PARAGRAPH", number="1", parent=self.code,
                                      kind="SECTION", marker_x=line.x0, flags=["IMPLICIT_NUMBER"]))
                self.stack = [self.root, node]
                self._append(node, text, line, True)
                return 1
        m = SECTION_RE.match(text)
        if m:
            number = m.group(1)
            expected = self.last_unit[0] + 1 if self.last_unit else 1
            if num_key(number)[0] == expected:
                self.last_unit = num_key(number)
                node = self._new(Node(f"{self.code}_PA{number}", "PARAGRAPH", number=number, parent=self.code,
                                      kind="SECTION", marker_x=line.x0))
                self.stack = [self.root, node]
                self._append(node, text, line, True)
                return 1
        sm = SUBPARA_RE.match(text)
        para = None
        if sm and self.last_unit and int(sm.group(1)) == self.last_unit[0]:
            # the open paragraph; after a short right-aligned line was taken as the signature the stack no longer
            # holds it, so reopen paragraph N itself ("2.1" belongs to paragraph 2)
            para = self._unit_node() or self.nodes.get(f"{self.code}_PA{sm.group(1)}")
        if para is not None:
            if para not in self.stack:
                self.stack = [self.root, para]
            node = self._new(Node(f"{para.key}_SP{sm.group(2)}", "SUB_PARAGRAPH", number=f"{sm.group(1)}.{sm.group(2)}",
                                  parent=para.key, kind="SUBPARA", marker_x=line.x0))
            self.stack = self.stack[:self.stack.index(para) + 1] + [node]
            self._append(node, text, line, True)
            return 1
        # signature block: right-aligned lines after the last paragraph
        if self.last_unit and line.x0 > 0.45 * self.max_x and len(text) < 60 and (
                i + 1 >= len(lines) or all(l.x0 > 0.45 * self.max_x for l in lines[i:i + 2])):
            if top.level != "SIGNATURE":
                node = self._new(Node(f"{self.code}_SIGNATURE", "SIGNATURE", parent=self.code, kind="SIGNATURE",
                                      marker_x=line.x0))
                self.stack = [self.root, node]
                top = node
            self._append(top, text, line, True)
            return 1
        if self._unit_node() is not None:
            return self._handle_body_line(line, text, lines[i - 1] if i else None, 0)
        self._append(top, text, line, line.para_start is not False)
        return 1

    # ------------------------------------------------------------ finish
    def _rekey(self, key: str, new_parent: str) -> None:
        """Move node `key` (and its subtree) under `new_parent`, rewriting keys by prefix."""
        node = self.nodes[key]
        old_parent = self.nodes[node.parent]
        suffix = key[len(old_parent.key) + 1:].split("_")[-1] if key.startswith(old_parent.key + "_") else key.split("_")[-1]
        level_tag = re.match(r"[A-Z]+", suffix).group(0) if re.match(r"[A-Z]+", suffix) else suffix
        siblings = [self.nodes[c] for c in self.nodes[new_parent].children if self.nodes[c].level == node.level]
        new_key = f"{new_parent}_{level_tag}{len(siblings) + 1}"
        while new_key in self.nodes:
            new_key += "x"
        old_parent.children.remove(key)
        # insert after the last child that precedes it in document order
        kids = self.nodes[new_parent].children
        pos = len(kids)
        for i, c in enumerate(kids):
            if self.nodes[c].order > node.order:
                pos = i
                break
        kids.insert(pos, new_key)
        mapping = {}
        def walk(k):
            mapping[k] = new_key + k[len(key):]
            for c in self.nodes[k].children:
                walk(c)
        walk(key)
        for old, new in mapping.items():
            n = self.nodes.pop(old)
            n.key = new
            n.children = [mapping.get(c, c) for c in n.children]
            if old == key:
                n.parent = new_parent
                n.number = str(len(siblings) + 1)
            else:
                n.parent = mapping.get(n.parent, n.parent)
            self.nodes[new] = n
        self.order = [mapping.get(k, k) for k in self.order]

    def _lift_trailing_provisos(self) -> None:
        """A proviso/explanation after the LAST item of a list qualifies the whole list's parent
        (CGST s.16(2)(d) ':' + 'Provided that ...' -> belongs to s.16(2)). Mid-list ones stay."""
        for key in list(self.order):
            node = self.nodes.get(key)
            if node is None or node.kind not in ("ALPHA", "ROMAN", "UPPER"):
                continue
            parent = self.nodes[node.parent]
            same = [c for c in parent.children if self.nodes[c].kind == node.kind]
            if not same or same[-1] != key:
                continue
            for child in [c for c in node.children if self.nodes[c].level == "PROVISO"]:
                # only provisos that come after all of the item's own list children
                later_lists = [c for c in node.children if self.nodes[c].kind in LIST_KINDS
                               and self.nodes[c].order > self.nodes[child].order]
                if later_lists:
                    continue
                self.nodes[child].flags.append("LIFTED_FROM_" + node.key[len(parent.key) + 1:])
                if f"PROVISO_ATTACH_REVIEW" in node.flags:
                    node.flags = [f for f in node.flags if f != "PROVISO_ATTACH_REVIEW"]
                self._rekey(child, parent.key)

    def _finish(self) -> None:
        self._lift_trailing_provisos()
        stream_end = self.stream_len
        for key in reversed(self.order):
            node = self.nodes[key]
            own = [r for r in node.ranges]
            starts = [r[0] for r in own] + [self.nodes[c].start for c in node.children if self.nodes[c].start >= 0]
            ends = [r[1] for r in own] + [self.nodes[c].end for c in node.children if self.nodes[c].end >= 0]
            node.start = min(starts) if starts else -1
            node.end = max(ends) if ends else -1
        del stream_end


def parse_layout(layout: LayoutDocument, code: str, doc_type: str) -> ParsedDocument:
    return Parser(layout, code, doc_type).parse()
