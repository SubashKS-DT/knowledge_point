"""IKP / Step 1 - layout-aware PDF reading.

Turns a legal PDF into clean BODY lines in reading order, and separates out:
  * running headers / footers / page numbers  -> dropped (but counted)
  * margin notes (Gazette-style section headings, "52 of 1962.")  -> MarginNote
  * footnotes at the page bottom ("16 Substituted for ... w.e.f. ...") -> Footnote
  * superscript footnote markers inside the text ("...(2)]16, the")  -> Line.fn_markers

No LLM is used. Every decision is based on font size, flags, position and
repetition, so the same PDF always gives the same result.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pymupdf as fitz

SUPERSCRIPT_FLAG = 1
ITALIC_FLAG = 2
BOLD_FLAG = 16

# CGST PDFs print quotes as U+2015 / U+2016. Normalise to straight quotes so
# later passes can match words reliably. Order matters: longer first.
CHAR_MAP = {
    "―": '"', "‖": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
    " ": " ", " ": " ", " ": " ", " ": " ", "​": "", "﻿": "",
    "­": "",
}
_CHAR_RE = re.compile("|".join(map(re.escape, CHAR_MAP)))


def normalise_text(text: str) -> str:
    text = _CHAR_RE.sub(lambda m: CHAR_MAP[m.group(0)], text)
    return re.sub(r"[ \t]+", " ", text)


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold_start: bool = False
    italic: bool = False
    fn_markers: List[Tuple[int, str]] = field(default_factory=list)  # (offset in text, number)
    para_start: Optional[bool] = None  # True new paragraph, False continuation, None unknown (page top)
    first_x: float = 0.0  # x of the first span (marker column)


@dataclass
class Footnote:
    number: str
    page: int
    text: str


@dataclass
class MarginNote:
    page: int
    x0: float
    y0: float
    y1: float
    side: str
    text: str


@dataclass
class LayoutDocument:
    page_count: int
    body_size: float
    lines: List[Line]
    footnotes: List[Footnote]
    margin_notes: List[MarginNote]
    dropped: List[Tuple[int, str, str]]  # (page, reason, text)
    toc_pages: List[int]
    raw_chars: int

    def footnote_text(self, number: str, page: int) -> Optional[Footnote]:
        """Resolve marker N on page p to the footnote N printed on p (or the next 2 pages)."""
        best = None
        for fn in self.footnotes:
            if fn.number == number and page <= fn.page <= page + 2:
                if best is None or fn.page < best.page:
                    best = fn
        return best


# ---------------------------------------------------------------- raw spans

@dataclass
class _Raw:
    page: int
    spans: list
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def text(self) -> str:
        return "".join(s["text"] for s in self.spans)


def _raw_lines(page, number: int) -> List[_Raw]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"] != ""]
            if not spans or not "".join(s["text"] for s in spans).strip():
                continue
            x0, y0, x1, y1 = line["bbox"]
            out.append(_Raw(number, spans, x0, y0, x1, y1))
    return out


def _span_size(spans) -> float:
    """Dominant font size of a line, ignoring tiny superscripts."""
    weights = Counter()
    for s in spans:
        if s["text"].strip():
            weights[round(s["size"] * 2) / 2] += len(s["text"].strip())
    return weights.most_common(1)[0][0] if weights else 0.0


def _body_size(all_raw: List[_Raw]) -> float:
    weights = Counter()
    for r in all_raw:
        for s in r.spans:
            n = len(s["text"].strip())
            if n:
                weights[round(s["size"] * 2) / 2] += n
    return weights.most_common(1)[0][0]


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip().lower()


AMENDMENT_WORDS = re.compile(r"(?i)\b(inserted|substituted|omitted|w\.?\s?e\.?\s?f|vide|notf|brought into force|"
                             r"amendment\) act|finance act|order no\.)")
TOC_TITLE = re.compile(r"(?i)\b(table of contents|contents|arrangement of (sections|rules|clauses))\b")
TOC_LINE = re.compile(r"(\.{4,}|…{2,}|\s{6,}\d{1,4}\s*$|\.{2,}\s*\d{1,4}\s*$)")


def _is_toc_page(lines: List[_Raw], previous_was_toc: bool = False) -> bool:
    text_lines = [r.text.strip() for r in lines if r.text.strip()]
    if not text_lines:
        return False
    head = " ".join(text_lines[:10])
    leaders = sum(1 for t in text_lines if TOC_LINE.search(t))
    chapters = {m.group(1) for t in text_lines for m in [re.match(r"^\s*CHAPTER\s+([IVXLC]+)\b", t)] if m}
    if TOC_TITLE.search(head) and (leaders >= 3 or len(chapters) >= 3):
        return True
    ending_digits = sum(1 for t in text_lines if re.search(r"\S\s+\d{1,4}\s*$", t))
    if len(chapters) >= 3 and ending_digits >= 3:  # chapter list with page numbers
        return True
    if previous_was_toc and (leaders >= 2 or len(chapters) >= 1) and leaders >= 0.2 * len(text_lines):
        return True
    return leaders >= max(6, int(0.45 * len(text_lines)))


# ---------------------------------------------------------------- main entry

def read_layout(pdf_path) -> LayoutDocument:
    with fitz.open(pdf_path) as pdf:
        if pdf.page_count == 0:
            raise ValueError(f"Empty PDF: {pdf_path}")
        pages = []
        for i in range(pdf.page_count):
            page = pdf.load_page(i)
            pages.append((i + 1, page.rect.width, page.rect.height, _raw_lines(page, i + 1)))
    all_raw = [r for _, _, _, rs in pages for r in rs]
    raw_chars = sum(len(r.text.strip()) for r in all_raw)
    if raw_chars < 50:
        raise ValueError(f"No extractable text in {pdf_path} (scanned PDF needs OCR first)")
    body = _body_size(all_raw)
    small_limit = body * 0.9

    # ---- running headers / footers: same normalised text in the top/bottom band on >= 2 pages
    # Footnotes are small type and are never treated as headers. Top band needs 2 pages,
    # bottom band 3 (the last body line of a page is often in the bottom band).
    band_count: Dict[Tuple[str, str], set] = defaultdict(set)
    for number, _, height, raws in pages:
        for r in raws:
            if _span_size(r.spans) <= small_limit:
                continue
            if r.y0 < 0.1 * height:
                band_count[("TOP", _norm_key(r.text))].add(number)
            elif r.y0 > 0.9 * height:
                band_count[("BOTTOM", _norm_key(r.text))].add(number)
    # Only real header text (>= 4 letters) - never a bare legal marker such as "(2)" or "(iii)".
    repeated = {(band, k) for (band, k), v in band_count.items()
                if len(v) >= (2 if band == "TOP" else 3) and len(re.findall(r"[a-z]", k)) >= 4
                and not re.match(r"^\[?\(?[#a-z]{1,4}\)", k)}

    dropped: List[Tuple[int, str, str]] = []
    lines: List[Line] = []
    footnotes: List[Footnote] = []
    margins: List[MarginNote] = []
    toc_pages: List[int] = []
    pending_markers: set = set()
    last_fn = 0

    for number, width, height, raws in pages:
        if _is_toc_page(raws, bool(toc_pages) and toc_pages[-1] == number - 1):
            toc_pages.append(number)
            for r in raws:
                dropped.append((number, "TOC", r.text.strip()))
            continue
        keep: List[_Raw] = []
        for r in raws:
            band = "TOP" if r.y0 < 0.1 * height else "BOTTOM" if r.y0 > 0.9 * height else ""
            key = _norm_key(r.text)
            if band and _span_size(r.spans) > small_limit and (
                    (band, key) in repeated or re.fullmatch(r"(page\s*)?#(\s*of\s*#)?|-\s*#\s*-", key)):
                dropped.append((number, "HEADER_FOOTER", r.text.strip()))
                continue
            keep.append(r)

        normal = [r for r in keep if _span_size(r.spans) > small_limit]
        if normal:
            xs0 = sorted(r.x0 for r in normal)
            xs1 = sorted(r.x1 for r in normal)
            left = xs0[max(0, int(len(xs0) * 0.05))]
            right = xs1[min(len(xs1) - 1, int(len(xs1) * 0.95))]
        else:
            left, right = 0, width

        page_lines: List[Line] = []
        page_margin: List[_Raw] = []
        ordered = sorted(keep, key=lambda r: (round(r.y0, 1), r.x0))
        body_rows = [r for r in ordered if not (_span_size(r.spans) <= small_limit and (r.x1 <= left + 2 or r.x0 >= right - 2))]
        # Footnote zone = trailing run of small-type lines in the lower part of the page that
        # either starts with a footnote number or reads like an amendment note. A run of small
        # table rows (tariff annexure) does not qualify.
        zone_start = len(body_rows)
        while zone_start > 0 and _span_size(body_rows[zone_start - 1].spans) <= small_limit \
                and body_rows[zone_start - 1].y0 > 0.4 * height:
            zone_start -= 1
        zone = body_rows[zone_start:]
        zone_text = " ".join(r.text for r in zone)
        zone_ok = bool(zone) and zone_start > 0 and (
            any(re.match(r"^\s*\d{1,4}\s*\S", r.text) and (re.match(r"^\s*(\d{1,4})", r.text).group(1) in pending_markers)
                for r in zone) or AMENDMENT_WORDS.search(zone_text))
        zone_ids = {id(r) for r in zone} if zone_ok else set()
        in_footnotes = False
        current_fn: Optional[Footnote] = footnotes[-1] if footnotes else None
        for r in ordered:
            size = _span_size(r.spans)
            small = size <= small_limit
            text = r.text.strip()
            if small and (r.x1 <= left + 2 or r.x0 >= right - 2):
                page_margin.append(r)
                continue
            # footnote start: leading number that matches a pending marker or the next expected number
            m = re.match(r"^\s*(\d{1,4})\s*(?=\S)", text)
            if small and m and r.y0 > 0.4 * height:
                n = m.group(1)
                if n in pending_markers or last_fn < int(n) <= last_fn + 3:
                    current_fn = Footnote(n, number, text[m.end():].strip())
                    footnotes.append(current_fn)
                    pending_markers.discard(n)
                    last_fn = max(last_fn, int(n))
                    in_footnotes = True
                    continue
            if small and current_fn is not None and (in_footnotes or id(r) in zone_ids):
                # continuation (possibly of a footnote that began on the previous page)
                current_fn.text = (current_fn.text + " " + text).strip()
                in_footnotes = True
                continue
            in_footnotes = False
            # body line: strip superscript footnote markers and remember where they were
            pieces, markers = [], []
            cursor = 0
            main = [s for s in r.spans if s["text"].strip() and s["size"] > small_limit]
            baseline = statistics.median(s["origin"][1] for s in main) if main else None
            for s in r.spans:
                st = s["text"]
                # Footnote marker = small digits raised ABOVE the baseline. Digits that sit on or
                # below it are subscripts in formulas (rule 42: "C2 = C1 - T4") and stay as text.
                raised = baseline is not None and s["origin"][1] < baseline - 1.0
                is_marker = bool(re.fullmatch(r"\s*\d{1,4}\s*", st)) and raised and (
                    (s["flags"] & SUPERSCRIPT_FLAG) or s["size"] <= body * 0.75) and size > small_limit
                if is_marker:
                    markers.append((cursor, st.strip()))
                    pending_markers.add(st.strip())
                    continue
                st = normalise_text(st)
                pieces.append(st)
                cursor += len(st)
            raw_text = "".join(pieces)
            lead = len(raw_text) - len(raw_text.lstrip())
            line_text = raw_text.strip()
            markers = [(max(0, min(len(line_text), off - lead)), n) for off, n in markers]
            if not line_text:
                continue
            # bold test uses the first span with a letter/digit: '[' before '59.' is often not bold
            first = next((s for s in r.spans if re.search(r"[A-Za-z0-9]", s["text"])),
                         next(s for s in r.spans if s["text"].strip()))
            page_lines.append(Line(number, r.x0, r.y0, r.x1, r.y1, line_text, size,
                bold_start=bool(first["flags"] & BOLD_FLAG), italic=bool(first["flags"] & ITALIC_FLAG),
                fn_markers=markers, first_x=r.x0))
        lines.extend(_merge_same_row(page_lines))
        margins.extend(_margin_notes(page_margin, left, right))

    _mark_paragraphs(lines)
    return LayoutDocument(len(pages), body, lines, footnotes, margins, dropped, toc_pages, raw_chars)


def _merge_same_row(lines: List[Line]) -> List[Line]:
    """'(a)' at x=106 and 'text' at x=142 on the same baseline become one line '(a) text'."""
    lines = sorted(lines, key=lambda l: (l.y0, l.x0))
    rows: List[List[Line]] = []
    for line in lines:
        if rows:
            ref = rows[-1][0]
            tol = 0.45 * max(line.size, ref.size)
            same_row = abs(line.y0 - ref.y0) < tol or abs(line.y1 - ref.y1) < tol
            overlaps = any(not (line.x0 >= o.x1 - 1 or line.x1 <= o.x0 + 1) for o in rows[-1])
            if same_row and not overlaps:
                rows[-1].append(line)
                continue
        rows.append([line])
    out = []
    for row in rows:
        row.sort(key=lambda l: l.x0)
        base = row[0]
        text, markers = base.text, list(base.fn_markers)
        for nxt in row[1:]:
            sep = " " if not text.endswith((" ", "-", "—", "–")) else ""
            offset = len(text) + len(sep)
            text += sep + nxt.text
            markers += [(offset + o, n) for o, n in nxt.fn_markers]
        out.append(Line(base.page, base.x0, min(l.y0 for l in row), max(l.x1 for l in row),
            max(l.y1 for l in row), text, base.size, base.bold_start or any(l.bold_start for l in row[:2]),
            base.italic, markers, None, base.x0))
    return out


def _margin_notes(raws: List[_Raw], left: float, right: float) -> List[MarginNote]:
    notes: List[MarginNote] = []
    for r in sorted(raws, key=lambda r: (r.x0 >= right - 2, r.y0)):
        side = "RIGHT" if r.x0 >= right - 2 else "LEFT"
        text = normalise_text(r.text).strip()
        if notes and notes[-1].side == side and notes[-1].page == r.page and r.y0 - notes[-1].y1 < 6:
            prev = notes[-1]
            joiner = "" if prev.text.endswith("-") else " "
            prev.text = (prev.text + joiner + text).strip()
            prev.y1 = r.y1
        else:
            notes.append(MarginNote(r.page, r.x0, r.y0, r.y1, side, text))
    return notes


def _mark_paragraphs(lines: List[Line]) -> None:
    by_page: Dict[int, List[Line]] = defaultdict(list)
    for line in lines:
        by_page[line.page].append(line)
    for page_lines in by_page.values():
        diffs = [b.y0 - a.y0 for a, b in zip(page_lines, page_lines[1:]) if 0 < b.y0 - a.y0 < 3 * a.size]
        pitch = statistics.median(diffs) if diffs else page_lines[0].size * 1.4
        page_lines[0].para_start = None
        for a, b in zip(page_lines, page_lines[1:]):
            b.para_start = (b.y0 - a.y0) > pitch * 1.18
