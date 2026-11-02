"""IKP / Step 3 - deterministic enrichment of parsed nodes.

* footnotes -> amendment history per node (Inserted / Substituted / Omitted, instrument, w.e.f. date)
* status    -> ACTIVE / INSERTED / SUBSTITUTED / OMITTED / HISTORICAL
* role      -> HEADING / LEAD_IN / PROVISION / CLOSING / NON_OPERATIVE / HISTORICAL
* XREF      -> explicit textual references ("sub-section (2) of section 16", "rule 46", "Schedule III")
* DEF       -> defined terms ('"supply" includes', 'the expression "x" means')
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .layout import LayoutDocument
from .structure import Node, ParsedDocument

NON_OPERATIVE = {"FRONT", "ANNEX", "SIGNATURE", "SUBJECT", "NOTE", "DOCUMENT", "ANNEXURE"}
CONTAINERS = {"CHAPTER", "SCHEDULE", "PART"}

ACT_NAMES = [
    (re.compile(r"(?i)Central Goods and Services Tax Act"), "CGSTA17"),
    (re.compile(r"(?i)Integrated Goods and Services Tax Act"), "IGSTA17"),
    (re.compile(r"(?i)Union Territory Goods and Services Tax Act"), "UTGSTA17"),
    (re.compile(r"(?i)Goods and Services Tax \(Compensation to States\) Act"), "GSTCA17"),
    (re.compile(r"(?i)Central Goods and Services Tax Rules"), "CGSTR17"),
    (re.compile(r"(?i)State Goods and Services Tax Act"), "SGST"),
    (re.compile(r"(?i)Customs Tariff Act"), "EXT:CUSTOMS_TARIFF_ACT"),
    (re.compile(r"(?i)Customs Act"), "EXT:CUSTOMS_ACT"),
    (re.compile(r"(?i)Companies Act"), "EXT:COMPANIES_ACT"),
    (re.compile(r"(?i)Income[- ]tax Act"), "EXT:INCOME_TAX_ACT"),
    (re.compile(r"(?i)Code of Civil Procedure"), "EXT:CPC"),
    (re.compile(r"(?i)Code of Criminal Procedure"), "EXT:CRPC"),
]


@dataclass
class Amendment:
    footnote: str
    page: int
    kind: str
    effective: str
    instrument: str
    text: str


@dataclass
class XRef:
    source: str
    target: str
    cue: str
    ref_type: str
    target_doc: str
    external: bool
    resolved: bool


@dataclass
class Definition:
    term: str
    element: str
    kind: str  # MEANS / INCLUDES / SAME_MEANING
    cue: str


@dataclass
class Enriched:
    amendments: Dict[str, List[Amendment]] = field(default_factory=dict)
    xrefs: List[XRef] = field(default_factory=list)
    definitions: List[Definition] = field(default_factory=list)
    unresolved_markers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- footnotes

def classify_footnote(text: str) -> Amendment:
    t = text.strip()
    kind = "NOTE"
    m = re.match(r"(?i)^\W*(inserted|substituted|omitted|added|renumbered|amended)", t)
    if m:
        kind = {"added": "INSERTED", "amended": "SUBSTITUTED"}.get(m.group(1).lower(), m.group(1).upper())
    elif re.search(r"(?i)\bomitted\b", t[:80]):
        kind = "OMITTED"
    elif re.search(r"(?i)\bsubstituted\b", t[:80]):
        kind = "SUBSTITUTED"
    elif re.search(r"(?i)\binserted\b", t[:80]):
        kind = "INSERTED"
    eff = ""
    em = re.search(r"(?i)(?:w\.?\s*e\.?\s*f\.?|with effect from|effective from|brought into force(?: w\.?e\.?f\.?)?(?: on)?)"
                   r"\s*(\d{1,2}(?:st|nd|rd|th)?[\s.\-/]+(?:\w+|\d{1,2})[\s.,\-/]+\d{4}|\d{2}\.\d{2}\.\d{4})", t)
    if em:
        eff = re.sub(r"\s+", " ", em.group(1)).strip()
    inst = ""
    im = re.search(r"(?i)(Notf\.?\s*No\.?\s*[\w./\-]+(?:\s*[-–]\s*\w+)?\s*(?:dt\.?|dated)\s*[\d.]+|"
                   r"Notification\s+No\.?\s*[\w./\-]+[^,;]*?dated\s*[\w ,.]+?\d{4}|"
                   r"The [A-Z][\w\s()]+?Act,\s*\d{4}\s*\(No\.\s*\d+\s*of\s*\d{4}\)|"
                   r"Finance(?: \(No\. ?\d\))? Act,\s*\d{4}(?:\s*\(No\.\s*\d+\s*of\s*\d{4}\))?|Order No\.?\s*[\w./\-]+)", t)
    if im:
        inst = re.sub(r"\s+", " ", im.group(1)).strip()
    return Amendment("", 0, kind, eff, inst, t)


# ---------------------------------------------------------------- references

def _act_after(text: str, pos: int) -> Optional[str]:
    """'section 5 of the Integrated Goods and Services Tax Act' -> 'IGSTA17'"""
    window = text[pos:pos + 120]
    m = re.match(r"\s*(?:,\s*)?(?:of|under)\s+(?:the\s+)?([A-Z][A-Za-z()&\s\-]+?(?:Act|Rules|Code))", window)
    if not m:
        return None
    name = m.group(1)
    if re.match(r"(?i)(said|this)\b", name):
        return None
    for pattern, code in ACT_NAMES:
        if pattern.search(name):
            return code
    return "EXT:" + re.sub(r"\W+", "_", name.strip()).upper()[:40]


REF_PATTERNS = [
    ("CLAUSE_SS", re.compile(r"(?i)\bclause\s*\(\s*(\w{1,4})\s*\)\s*of\s*sub-section\s*\(\s*(\w{1,4})\s*\)\s*of\s*section\s*(\d{1,3}[A-Z]{0,2})\b")),
    ("CLAUSE_SR", re.compile(r"(?i)\bclause\s*\(\s*(\w{1,4})\s*\)\s*of\s*sub-rule\s*\(\s*(\w{1,4})\s*\)\s*of\s*rule\s*(\d{1,3}[A-Z]{0,2})\b")),
    ("SUB_SECTION", re.compile(r"(?i)\bsub-sections?\s*\(\s*(\w{1,4})\s*\)\s*of\s*section\s*(\d{1,3}[A-Z]{0,2})\b")),
    ("SUB_RULE", re.compile(r"(?i)\bsub-rules?\s*\(\s*(\w{1,4})\s*\)\s*of\s*rule\s*(\d{1,3}[A-Z]{0,2})\b")),
    ("CLAUSE_S", re.compile(r"(?i)\bclause\s*\(\s*(\w{1,4})\s*\)\s*of\s*section\s*(\d{1,3}[A-Z]{0,2})\b")),
    ("SECTION", re.compile(r"(?i)\bsections?\s+(\d{1,3}[A-Z]{0,2})\b(?!\s*\()")),
    ("SECTION_SS", re.compile(r"(?i)\bsection\s+(\d{1,3}[A-Z]{0,2})\s*\(\s*(\w{1,4})\s*\)")),
    ("RULE", re.compile(r"(?i)\brules?\s+(\d{1,3}[A-Z]{0,2})\b(?!\s*\()")),
    ("RULE_SR", re.compile(r"(?i)\brule\s+(\d{1,3}[A-Z]{0,2})\s*\(\s*(\w{1,4})\s*\)")),
    ("SCHEDULE", re.compile(r"\bSchedule\s+([IVX]{1,4})\b")),
    ("LOCAL_SS", re.compile(r"(?i)\bsub-section\s*\(\s*(\w{1,4})\s*\)(?!\s*of\s*section)")),
    ("LOCAL_SR", re.compile(r"(?i)\bsub-rule\s*\(\s*(\w{1,4})\s*\)(?!\s*of\s*rule)")),
]


def _unit_of(doc: ParsedDocument, key: str) -> Optional[Node]:
    node = doc.nodes.get(key)
    while node is not None and node.level not in ("SECTION", "RULE", "ENTRY", "PARAGRAPH", "HISTORICAL"):
        node = doc.nodes.get(node.parent)
    return node


def find_xrefs(doc: ParsedDocument, key: str, text: str, tree=None) -> List[XRef]:
    """Explicit references, including plural / list / range forms, resolved by walking the tree."""
    from .citations import REF_RE, _singular, convention_key, find_child, parse_tail, split_list, walk
    code = doc.code
    nodes_d, children = tree if tree else _tree(doc)
    unit = _unit_of(doc, key)
    out: List[XRef] = []
    for m in REF_RE.finditer(text):
        kind = _singular(m.group("kind"))
        if kind in ("proviso", "explanation"):
            continue
        act = _act_after(text, m.end())
        tail_unit, tail_chain = parse_tail(m.group("tail") or "")
        for item in split_list(m.group("list")):
            if kind in ("section", "rule", "schedule"):
                unit_kind, unit_no, steps = kind, item[0], [("chain", t) for t in item[1:]]
            elif tail_unit:
                unit_kind, unit_no = tail_unit
                steps = [(k, t) for k, t in tail_chain] + [(kind, t) for t in item]
            else:
                # local reference: 'sub-section (1)', 'clause (a)', 'sub-rule (2)' within the same unit
                if unit is None or kind in ("paragraph", "para", "item", "entry"):
                    continue
                if (kind == "sub-section" and doc.doc_type != "ACT") or (kind == "sub-rule" and doc.doc_type != "RULES"):
                    continue
                target, ok = None, False
                anc = doc.nodes.get(key)
                local_steps = [(k, t) for k, t in tail_chain] + [(kind, t) for t in item]
                while anc is not None:
                    hit, ok = walk(nodes_d, children, anc.key, local_steps)
                    if ok:
                        target = hit
                        break
                    if anc.key == unit.key:
                        break
                    anc = doc.nodes.get(anc.parent)
                out.append(XRef(key, target or f"{unit.key}_?{''.join(item)}", m.group(0).strip(), "LOCAL_" + kind.upper(),
                                code, False, ok))
                continue
            if unit_kind == "schedule":
                default = code if doc.doc_type == "ACT" else "CGSTA17"
            elif unit_kind == "rule":
                default = code if doc.doc_type == "RULES" else "CGSTR17"
            else:
                default = code if doc.doc_type == "ACT" else "CGSTA17"
            target_doc = act or default
            chain_tokens = [t for _, t in steps]
            conv = convention_key(target_doc, unit_kind, unit_no, chain_tokens)
            if target_doc == code:
                unit_key = convention_key(code, unit_kind, unit_no, [])
                if unit_key in nodes_d:
                    hit, ok = walk(nodes_d, children, unit_key, steps)
                    out.append(XRef(key, hit if ok else conv, m.group(0).strip(), unit_kind.upper() + ("_PART" if steps else ""),
                                    target_doc, False, ok))
                else:
                    out.append(XRef(key, conv, m.group(0).strip(), unit_kind.upper(), target_doc, False, False))
            else:
                out.append(XRef(key, conv, m.group(0).strip(), unit_kind.upper() + ("_PART" if steps else ""),
                                target_doc, True, False))
    return out


def _tree(doc: ParsedDocument):
    nodes_d = {k: {"level": n.level, "number": n.number} for k, n in doc.nodes.items()}
    children = {k: list(n.children) for k, n in doc.nodes.items()}
    return nodes_d, children


DEF_HEAD = re.compile(r'^\[?\s*\(\s*\w{1,4}\s*\)\s*\[?\s*"([^"]{1,90})"\s*(?:\]\s*)?(?:,[^,]{0,80},\s*)?(?:in relation to [^,]{0,80}?\s*)?'
                      r'(means|includes|shall mean|shall include|shall have|has the meaning)', re.I)
DEF_INLINE = re.compile(r'(?i)\bthe (?:expression|term|word)s?\s*"([^"]{1,90})"\s*(?:,[^"]{0,60})?\s*(means|includes|shall mean|shall include|shall have)')
DEF_REFERRED = re.compile(r'(?i)\b(?:hereafter|hereinafter)\s+referred\s+to\s+as\s+(?:the\s+)?"([^"]{1,60})"')


def find_definitions(key: str, text: str, level: str) -> List[Definition]:
    out = []
    if level in ("CLAUSE", "SUB_CLAUSE"):
        m = DEF_HEAD.match(text)
        if m:
            kind = "MEANS" if "mean" in m.group(2).lower() else "INCLUDES" if "include" in m.group(2).lower() else "SAME_MEANING"
            out.append(Definition(m.group(1).strip(), key, kind, m.group(0)[:120]))
    for m in DEF_INLINE.finditer(text):
        kind = "MEANS" if "mean" in m.group(2).lower() else "INCLUDES" if "include" in m.group(2).lower() else "SAME_MEANING"
        out.append(Definition(m.group(1).strip(), key, kind, m.group(0)[:120]))
    for m in DEF_REFERRED.finditer(text):
        out.append(Definition(m.group(1).strip(), key, "REFERRED_AS", m.group(0)[:120]))
    return out


# ---------------------------------------------------------------- main

def enrich(doc: ParsedDocument, layout: LayoutDocument) -> Enriched:
    result = Enriched()
    # every node's own ranges, sorted, for marker -> node lookup
    spans = sorted((a, b, key) for key, n in doc.nodes.items() for a, b in n.ranges)
    starts = [s[0] for s in spans]
    for offset, number, page in doc.marker_offsets:
        i = bisect.bisect_right(starts, offset) - 1
        key = None
        while i >= 0:
            a, b, k = spans[i]
            if a <= offset <= b:
                key = k
                break
            if b < offset - 2:
                break
            i -= 1
        if key is None:
            continue
        fn = layout.footnote_text(number, page)
        if fn is None:
            result.unresolved_markers.append(f"{key}:{number}@p{page}")
            continue
        amend = classify_footnote(fn.text)
        amend.footnote, amend.page = number, fn.page
        node = doc.nodes[key]
        if number not in node.footnotes:
            node.footnotes.append(number)
            result.amendments.setdefault(key, []).append(amend)

    in_history, in_annex = set(), set()
    for key in doc.order:
        node = doc.nodes[key]
        parent = doc.nodes.get(node.parent)
        if node.level == "HISTORICAL" or (parent is not None and parent.key in in_history):
            in_history.add(key)
        if node.level == "ANNEX" or (parent is not None and parent.key in in_annex):
            in_annex.add(key)  # Removal of Difficulty Orders etc. - kept, structured, not operative law of this Act

    tree = _tree(doc)
    for key in doc.order:
        node = doc.nodes[key]
        own = doc.own_text(key)
        # ---- status
        body = re.sub(r"^\[?\s*(\(\s*\w{1,7}\s*\)|\d{1,3}[A-Z]{0,3}\s*\.)\s*", "", own).strip()
        body = re.sub(r"^" + re.escape(node.heading) + r"\s*[.\-—–:]*", "", body).strip() if node.heading else body
        if key in in_history:
            node.status = "HISTORICAL"
        elif re.fullmatch(r"[\[\s]*\*[\s*]*\]?[.;,]?(\s*(or|and))?", body or "x"):
            node.status = "OMITTED"
        elif re.match(r"(?i)^\[?\s*omitted\s*\]?\.?$", body):
            node.status = "OMITTED"
        else:
            amends = result.amendments.get(key, [])
            if re.match(r"^\(\s*\w{1,7}\s*\)\s*\[", own):
                node.bracketed = True  # '(4) [No input tax credit ...]'
            if node.bracketed and not amends and node.children:
                # '[49A. ... ]130' - the closing bracket + marker sit at the end of the last child
                full = doc.stream[node.start:node.end]
                tail = [(o, n, p) for o, n, p in doc.marker_offsets if node.start <= o <= node.end
                        and doc.stream[max(node.start, o - 3):o].rstrip().endswith("]")]
                if tail:
                    o, number, page = tail[-1]
                    fn = layout.footnote_text(number, page)
                    if fn is not None:
                        a = classify_footnote(fn.text)
                        a.footnote, a.page = number, fn.page
                        amends = [a]
                        result.amendments.setdefault(key, []).append(a)
                        node.footnotes.append(number)
                del full
            if node.bracketed and amends:
                # the footnote that closes the node's opening '[' decides; others are partial edits
                closing = [a for a in amends if a.kind in ("INSERTED", "SUBSTITUTED", "OMITTED")]
                kind = closing[-1].kind if closing else "AMENDED"
                if kind == "OMITTED" and len(body) > 40:
                    # still printed in full: omission is future-dated / partial -> keep it, record the date
                    node.status = "ACTIVE"
                    node.flags.append("OMISSION_NOTED_WEF_" + (closing[-1].effective or "UNKNOWN").replace(" ", "_"))
                else:
                    node.status = kind
            elif amends:
                node.status = "ACTIVE"  # amended in parts; details in amendments
        # ---- role
        if node.level in NON_OPERATIVE or key in in_annex:
            node.role = "NON_OPERATIVE"
        elif key in in_history:
            node.role = "HISTORICAL"
        elif node.level in CONTAINERS:
            node.role = "HEADING"
        elif node.level == "CLOSING":
            node.role = "CLOSING"
        elif node.level in ("SECTION", "RULE", "ENTRY", "PARAGRAPH") and node.children:
            node.role = "HEADING" if len(body) < 3 else "LEAD_IN"
        elif node.children:
            node.role = "LEAD_IN"
        else:
            node.role = "PROVISION"
        # ---- references and definitions (operative text only)
        if node.role not in ("NON_OPERATIVE",) and own:
            result.xrefs.extend(find_xrefs(doc, key, own, tree))
            result.definitions.extend(find_definitions(key, own, node.level))
    return result
