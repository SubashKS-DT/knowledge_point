"""IKP building blocks: PDF -> layout -> structure -> enrichment -> per-chapter groups -> records.

Every node record carries
  * exact own text (with [[PAGE n]] markers) and full text (node + descendants)
  * blocks     : per page, the bounding box and PDF line ids the text came from
  * anchors    : first / last words of the node, so the exact block can be cut out of the PDF again
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .cluster import derive_clusters, to_rows as cluster_to_rows
from .enrich import Enriched, enrich
from .gates import QualityReport, evaluate
from .layout import LayoutDocument, read_layout
from .structure import ParsedDocument, parse_layout

PARSER_VERSION = "ikp-structural-2.1"
REF_ROLES = {"NON_OPERATIVE", "HISTORICAL"}
GROUP_LEVELS = {"FRONT", "CHAPTER", "SCHEDULE", "ANNEX", "PART"}


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_value(value) -> str:
    """RESULTS is 'key: value; key: value' - keep separators out of values."""
    return re.sub(r"\s+", " ", str(value)).strip().replace(";", ",")


def _results(fields: Dict[str, object]) -> str:
    return "; ".join(f"{k}: {_clean_value(v)}" for k, v in fields.items() if v not in (None, "", []))


def page_range_text(pages: List[int]) -> str:
    if not pages:
        return ""
    if len(pages) == 1:
        return str(pages[0])
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"{pages[0]}-{pages[-1]}"
    return ",".join(map(str, pages))


# ---------------------------------------------------------------- analysis

@dataclass
class Analysis:
    code: str
    doc_type: str
    name: str
    pdf_sha256: str
    layout: LayoutDocument
    parsed: ParsedDocument
    enriched: Enriched
    law_as_on: str = ""
    law_as_on_iso: str = ""


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


def _iso(text: str) -> str:
    t = text.strip().lower().replace(",", " ")
    m = re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:day\s+of\s+)?([a-z]+)\s+(\d{4})", t)
    if m and m.group(2) in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return ""


def detect_law_as_on(layout: LayoutDocument) -> Tuple[str, str]:
    """'AS ON 31.08.2021' / 'as amended up to ...' / circular 'dated 18th October, 2017'."""
    head = " ".join(l.text for l in layout.lines if l.page <= 3)[:6000]
    for pattern in (r"(?i)\bas\s+on\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
                    # notification / circular dateline: 'New Delhi, the 30th September, 2019' / 'New Delhi, Dated ...'
                    r"(?i)\bNew\s+Delhi\s*,?\s*(?:dated\s*[:\-]?\s*)?(?:the\s+)?"
                    r"(\d{1,2}\s*(?:st|nd|rd|th)?\s*[A-Za-z]+,?\s+\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{4})",
                    r"(?i)\bas\s+(?:amended|updated|modified)\s+(?:up\s*to|upto|till|on)\s+"
                    r"(\d{1,2}(?:st|nd|rd|th)?\s+\w+,?\s+\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{4})",
                    r"(?i)\bdated\s*(?:the\s+)?(\d{1,2}(?:st|nd|rd|th)?\s+\w+,?\s+\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{4})",
                    r"(?i)\bthe\s+(\d{1,2}(?:st|nd|rd|th)?\s+\w+,?\s+\d{4})"):
        m = re.search(pattern, head)
        if m:
            return m.group(1).strip(), _iso(m.group(1))
    return "", ""


def analyse(pdf_path, code: str, doc_type: str, name: str = "") -> Analysis:
    pdf_path = Path(pdf_path)
    layout = read_layout(pdf_path)
    parsed = parse_layout(layout, code, doc_type)
    enriched = enrich(parsed, layout)
    raw, iso = detect_law_as_on(layout)
    return Analysis(code, doc_type, name, file_sha256(pdf_path), layout, parsed, enriched, raw, iso)


def re_enrich(a: Analysis) -> None:
    """Recompute status / roles / xrefs / definitions after a structure change (LLM assist)."""
    for n in a.parsed.nodes.values():
        n.footnotes = []
        n.status = "ACTIVE"
    a.enriched = enrich(a.parsed, a.layout)


# ---------------------------------------------------------------- chapter groups

@dataclass
class ChapterGroup:
    key: str
    level: str
    label: str
    ord: int
    keys: List[str] = field(default_factory=list)  # group root + every descendant, document order


def chapter_groups(parsed: ParsedDocument) -> List[ChapterGroup]:
    root = parsed.nodes[parsed.code]
    groups: List[ChapterGroup] = []
    body: Optional[ChapterGroup] = None
    for child in root.children:
        node = parsed.nodes[child]
        if node.level in GROUP_LEVELS:
            groups.append(ChapterGroup(child, node.level, (node.heading or node.level)[:120], len(groups) + 1))
            body = None
        else:  # circulars / notifications: paragraphs hang off the root -> one BODY group
            if body is None:
                body = ChapterGroup(f"{parsed.code}_BODY", "BODY", "Document body", len(groups) + 1)
                groups.append(body)
            body.keys.append(child)
    owner: Dict[str, str] = {}
    for g in groups:
        for top in ([g.key] if g.level != "BODY" else list(g.keys)):
            stack = [top]
            while stack:
                k = stack.pop()
                owner[k] = g.key
                stack.extend(parsed.nodes[k].children)
    by_group: Dict[str, List[str]] = {g.key: [] for g in groups}
    for k in parsed.order:
        if k in owner:
            by_group[owner[k]].append(k)
    for g in groups:
        g.keys = by_group[g.key]
    return groups


# ---------------------------------------------------------------- per-node record

def _segments(doc: ParsedDocument, a: int, b: int):
    """Stream segments overlapping [a, b): (segment index, overlap start, overlap end)."""
    i = max(bisect.bisect_right(doc.line_offsets, a) - 1, 0)
    n = len(doc.line_offsets)
    while i < n and doc.line_offsets[i] < b:
        seg_start = doc.line_offsets[i]
        seg_end = doc.line_offsets[i + 1] if i + 1 < n else len(doc.stream)
        lo, hi = max(a, seg_start), min(b, seg_end)
        if lo < hi and doc.stream[lo:hi].strip():
            yield i, lo, hi
        i += 1


def text_with_pages(doc: ParsedDocument, ranges: List[List[int]]) -> str:
    """Own text with [[PAGE n]] markers wherever the physical page changes."""
    out, current = [], None
    for a, b in ranges:
        for i, lo, hi in _segments(doc, a, b):
            page = doc.line_pages[i]
            piece = doc.stream[lo:hi]
            if page != current:
                out.append(("\n" if out else "") + f"[[PAGE {page}]]\n")
                current = page
                piece = piece.lstrip()
            out.append(piece)
        out.append("\n")
    return re.sub(r"[ \t]+\n", "\n", "".join(out)).strip()


def blocks_of(doc: ParsedDocument, layout: LayoutDocument, ranges: List[List[int]]) -> List[dict]:
    """Per page: bounding box + PDF line ids (+ char span inside the first/last line)."""
    pages: Dict[int, dict] = {}
    for a, b in ranges:
        for i, lo, hi in _segments(doc, a, b):
            li = doc.seg_line[i] if i < len(doc.seg_line) else -1
            if li < 0:
                continue
            line = layout.lines[li]
            off = doc.seg_line_off[i] + (lo - doc.line_offsets[i])
            blk = pages.setdefault(line.page, {"page": line.page, "bbox": [line.x0, line.y0, line.x1, line.y1],
                                               "lines": [], "first_char": off})
            bb = blk["bbox"]
            blk["bbox"] = [min(bb[0], line.x0), min(bb[1], line.y0), max(bb[2], line.x1), max(bb[3], line.y1)]
            if li not in blk["lines"]:
                blk["lines"].append(li)
            blk["last_char"] = off + (hi - lo)
    out = []
    for p in sorted(pages):
        blk = pages[p]
        blk["bbox"] = [round(v, 1) for v in blk["bbox"]]
        out.append(blk)
    return out


def anchors(own_text: str, size: int = 60) -> Tuple[str, str]:
    plain = re.sub(r"\[\[PAGE \d+\]\]\n?", "", own_text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:size], plain[-size:]


def _path(doc: ParsedDocument, key: str, memo: Dict[str, str]) -> str:
    if key in memo:
        return memo[key]
    node = doc.nodes[key]
    if node.level == "DOCUMENT":
        memo[key] = ""
        return ""
    short = {"PROVISO": "P", "EXPLANATION": "E", "CLOSING": "CL", "ILLUSTRATION": "ILL", "HISTORICAL": "HIST",
             "ANNEXURE": "ANX", "NOTE": "NOTE", "TABLE": "TBL", "TABLE_ROW": "ROW", "ORDER": "O"}
    if node.level == "SCHEDULE":
        token = "SCH" + node.number
    elif node.level in short:
        token = short[node.level] + (node.number or "1")
    elif node.level in ("FRONT", "ANNEX", "SUBJECT", "SIGNATURE"):
        token = node.level
    else:
        token = node.number
    parent = _path(doc, node.parent, memo) if node.parent in doc.nodes else ""
    memo[key] = ".".join(t for t in (parent, token) if t)
    return memo[key]


def group_records(a: Analysis, group: ChapterGroup) -> Tuple[List[dict], List[dict]]:
    """Index rows (5 columns) + node records for one chapter group."""
    doc, layout, nodes = a.parsed, a.layout, a.parsed.nodes
    order = [k for k in doc.order if k != doc.code]
    position = {k: i for i, k in enumerate(order)}
    memo: Dict[str, str] = {}
    rows, texts = [], []
    keyset = set(group.keys)
    for key in group.keys:
        node = nodes[key]
        i = position[key]
        nxt = next((k for k in order[i + 1:] if not k.startswith(key + "_")), "NA")
        pages = doc.pages_of(node.start, node.end) if node.start >= 0 else []
        own = text_with_pages(doc, node.ranges) if node.ranges else ""
        start_anchor, end_anchor = anchors(own)
        amends = a.enriched.amendments.get(key, [])
        fields = {
            "level": node.level, "number": node.number, "heading": node.heading, "parent": node.parent,
            "role": node.role, "status": node.status, "pages": pages, "footnotes": ",".join(node.footnotes),
            "path": _path(doc, key, memo), "next_element": nxt,
            "next_page_number": page_range_text(doc.pages_of(nodes[nxt].start, nodes[nxt].start + 1)) if nxt in nodes else "NA",
            "chapter": group.key, "flags": ",".join(sorted(set(node.flags))), "index_order": i + 1, "origin": "PARSER",
        }
        rows.append({"DOC": doc.code, "TYPE": "REF" if node.role in REF_ROLES else "NODE", "ELEMENT": key,
                     "PAGE_NUMBER": page_range_text(pages), "RESULTS": _results(fields)})
        texts.append({
            "doc": doc.code, "element": key, "chapter": group.key, "level": node.level, "number": node.number,
            "heading": node.heading, "parent": node.parent, "children": list(node.children), "role": node.role,
            "status": node.status, "path": fields["path"], "ord": i + 1,
            "page_start": pages[0] if pages else None, "page_end": pages[-1] if pages else None,
            "start_char": node.start if node.start >= 0 else None,
            "end_char": node.end if node.end >= 0 else None,
            "blocks": blocks_of(doc, layout, node.ranges), "start_anchor": start_anchor, "end_anchor": end_anchor,
            "own_text": own, "full_text": doc.full_text(key),
            "footnotes": node.footnotes, "flags": sorted(set(node.flags)),
            "amendments": [asdict(x) for x in amends],
            "xrefs": [{"to": x.target, "cue": x.cue, "type": x.ref_type, "target_doc": x.target_doc,
                       "external": x.external, "resolved": x.resolved} for x in a.enriched.xrefs if x.source == key],
            "text_sha256": hashlib.sha256(own.encode("utf-8")).hexdigest(),
        })
    for x in a.enriched.xrefs:
        if x.source in keyset:
            rows.append({"DOC": doc.code, "TYPE": "XREF", "ELEMENT": x.source, "PAGE_NUMBER": "",
                         "RESULTS": _results({"to": x.target, "cue": x.cue, "type": x.ref_type, "target_doc": x.target_doc,
                                              "external": str(x.external).lower(), "resolved": str(x.resolved).lower()})})
    for d in a.enriched.definitions:
        if d.element in keyset:
            rows.append({"DOC": doc.code, "TYPE": "DEF", "ELEMENT": d.term, "PAGE_NUMBER": "",
                         "RESULTS": _results({"defined_in": d.element, "kind": d.kind, "cue": d.cue})})
    # C_ / CM_ / UNC rows — one cluster per top-level operative provision
    c_cards, unc_keys = derive_clusters(doc, group)
    rows.extend(cluster_to_rows(doc.code, c_cards, unc_keys))
    return rows, texts


def document_records(a: Analysis, quality: QualityReport) -> Tuple[List[dict], List[dict]]:
    """DOC row, document-root NODE (its text is the instrument's own title line) and SUM row."""
    doc, layout, code = a.parsed, a.layout, a.code
    front = doc.full_text(f"{code}_FRONT") if f"{code}_FRONT" in doc.nodes else ""
    m = re.search(r"(?im)^.*\b(?:circular\s+no\.?|notification\s+no\.?|the\s+[a-z ()]+?\bact,\s*\d{4}|"
                  r"[a-z ()]+rules,\s*\d{4}).*$", front)
    title = m.group(0).strip() if m else (front.splitlines()[0].strip() if front else a.name)
    title = re.split(r"(?i)\s+as amended by|\s*[•;]", title)[0].strip()[:200]
    rows = [
        {"DOC": code, "TYPE": "DOC", "ELEMENT": code, "PAGE_NUMBER": f"1-{layout.page_count}",
         "RESULTS": _results({"instrument": code, "name": a.name, "type": a.doc_type, "pdf_sha256": a.pdf_sha256,
                              "parser": PARSER_VERSION, "law_as_on": a.law_as_on, "quality": quality.status})},
        {"DOC": code, "TYPE": "NODE", "ELEMENT": code, "PAGE_NUMBER": "1",
         "RESULTS": _results({"level": "DOCUMENT", "heading": a.name or code, "role": "NON_OPERATIVE",
                              "status": "ACTIVE", "pages": [1], "index_order": 0, "origin": "PARSER"})},
        {"DOC": code, "TYPE": "SUM", "ELEMENT": code, "PAGE_NUMBER": f"1-{layout.page_count}",
         "RESULTS": _results({**{k: v for k, v in quality.metrics.items() if not isinstance(v, list)},
                              "quality": quality.status, "issues": " | ".join(quality.issues)})},
    ]
    root = doc.nodes[code]
    texts = [{"doc": code, "element": code, "chapter": "_DOC", "level": "DOCUMENT", "number": "", "heading": a.name or code,
              "parent": "", "children": list(root.children), "role": "NON_OPERATIVE", "status": "ACTIVE", "path": "",
              "ord": 0, "page_start": 1, "page_end": layout.page_count, "blocks": [], "start_anchor": title[:60],
              "end_anchor": title[-60:], "own_text": f"[[PAGE 1]]\n{title}", "full_text": "", "footnotes": [],
              "flags": [], "amendments": [], "xrefs": [], "text_sha256": hashlib.sha256(title.encode()).hexdigest()}]
    return rows, texts


def document_quality(a: Analysis) -> QualityReport:
    return evaluate(a.parsed, a.layout, a.enriched)


def write_jsonl(path: Path, manifest: dict, texts: List[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_manifest": manifest}, ensure_ascii=False, default=str) + "\n")
        for rec in texts:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return path


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
