"""MKP / Python drafter - builds every MKP's TEXT and fixed fields straight from the IKP index.

The LLMs never write legal text. Python applies the fixed framing rules of the MKP prompt:
  CLAUSE RULE    clause = parent lead-in(s) + clause text + shared closing text, '; or' -> '.'
  HEADING RULE   "7. Scope of supply" -> "Section 7 deals with scope of supply."
  AMENDMENT RULE one AMENDMENT MKP per footnote, fixed templates + footnote text verbatim
  plus citation, citation_key (= IKP key), part, parent, pageno, valid_from, cross_refs, topic, heading_path.
The LLMs only add: concept name, knowledge_type, subtopic and the phrase split (see labels.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

UNIT_LEVELS = ("SECTION", "RULE", "ENTRY", "PARAGRAPH")
LIST_LEVELS = ("SUB_SECTION", "SUB_RULE", "CLAUSE", "SUB_CLAUSE", "ITEM", "SUB_ITEM", "SUB_PARAGRAPH")
SENTENCE_STARTERS = ("PROVISO", "EXPLANATION", "ILLUSTRATION", "CLOSING")
NOT_FRAMED = ("DOCUMENT", "CHAPTER", "SCHEDULE", "PART", "CLOSING", "TABLE", "ANNEXURE", "NOTE", "FRONT", "ANNEX",
              "SIGNATURE", "HISTORICAL", "ORDER")
ORDINAL = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


@dataclass
class DraftMKP:
    mkp_key: str                 # citation_key#part
    citation_key: str            # = IKP element key
    part: str                    # TEXT / AMD1 / AMD2 ...
    kind: str                    # TEXT / HEADING / AMENDMENT / METADATA
    text: str
    citation: str
    node_key: str
    level: str
    parent: str = "NA"
    topic: str = ""
    heading_path: List[str] = field(default_factory=list)
    pageno: str = ""
    valid_from: str = "NA"
    cross_refs: List[str] = field(default_factory=list)
    group: str = ""              # MKPs sharing one lead-in / closing share one subtopic
    fixed_knowledge_type: str = ""
    fixed_subtopic: str = ""
    fixed_phrases: List[str] = field(default_factory=list)
    source_keys: List[str] = field(default_factory=list)  # IKP nodes whose words make up the text


# ---------------------------------------------------------------- text helpers

def plain(text: str) -> str:
    t = re.sub(r"\[\[PAGE \d+\]\]\n?", " ", text or "")
    return re.sub(r"\s+", " ", t).strip()


def strip_label(node: dict) -> str:
    """Remove the printed label ('(a)', '7. Scope of supply.—', '2.') from a node's own text."""
    t = plain(node.get("own_text", ""))
    level = node["level"]
    if level in UNIT_LEVELS or level == "SUB_PARAGRAPH":
        t = re.sub(r"^\[?\s*(?:Rule\s+|Section\s+)?\d{1,3}[A-Z]{0,3}(?:\.\d{1,2})?\s*\.?\s*", "", t)
        heading = (node.get("heading") or "").strip()
        if heading and t.lower().startswith(heading.lower()):
            rest = t[len(heading):]
            marked = re.match(r"^\s*\.?\s*(?:[-—–―]+|:-|:)", rest)
            body = rest.strip(" .;")
            # Schedule entries: IKP may store the sentence itself (or its first line) as 'heading'.
            #   '3. Treatment or process Any treatment ...' -> real heading (text goes on with a capital / '(')
            #   '6. Actionable claims, other than ...'      -> no text after it, no children: the heading IS the text
            #   '5. Sale of land ... sale of' + 'building.' -> continues in lower case: the heading IS the text
            real = (marked or level in ("SECTION", "RULE", "ARTICLE")
                    or (body and re.match(r"^[\[(\"“]*[A-Z(]", body))
                    or (not body and node.get("children")))
            if real:
                t = re.sub(r"^\s*\.?\s*(?:[-—–―]+|:-|:)?\s*", "", rest)
    elif level in LIST_LEVELS or level in ("TABLE_ROW",):
        t = re.sub(r"^\[?\s*\(\s*\w{1,7}\s*\)\s*", "", t) if level != "TABLE_ROW" else t
    return t.strip()


def current_text(text: str) -> str:
    """Operative (current) wording: drop omission marks [****], drop amendment brackets but keep the words."""
    t = re.sub(r"\[\s*\[?\s*\*+\s*\]?\s*\]", " ", text)
    t = re.sub(r"\[\s*\*+\s*\]", " ", t)
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"\[([^\[\]]*)\]", r"\1", t)
    t = t.replace("[", "").replace("]", "")
    t = re.sub(r"\s+([,;:.])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def _lead_in(text: str) -> str:
    """'... the expression "supply" includes––' -> '... includes'; 'namely:-' -> 'namely:'"""
    t = re.sub(r"\s*(?:[-—–―]+|:\s*[-—–―]+)\s*$", lambda m: ":" if ":" in m.group(0) else "", text.strip())
    return t.strip()


def _item_end(text: str, followed: bool) -> str:
    t = text.strip()
    t = re.sub(r"\s*;\s*(?:or|and)\s*$", "", t)
    t = re.sub(r"\s+(?:or|and)\s*$", "", t) if re.search(r"[;,]\s+(?:or|and)\s*$", t) else t
    t = re.sub(r"\s*;\s*$", "", t)
    if not followed:
        t = re.sub(r"\s*[,:]\s*$", "", t)
    return t


def sentence(parts: List[str]) -> str:
    parts = [p for p in (x.strip() for x in parts) if p]
    if not parts:
        return ""
    t = " ".join(parts)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*[;,:]+\s*$", "", t)
    if not re.search(r"[.?!]\"?$", t):
        t += "."
    return t[0].upper() + t[1:]


# ---------------------------------------------------------------- descriptions / citations

def _num(node: dict) -> str:
    return str(node.get("number") or "").strip("() .")


def _chain(idx, key: str) -> List[dict]:
    """Node and its ancestors up to (and including) the unit, nearest first."""
    out, k = [], key
    while k and k in idx.nodes:
        n = idx.nodes[k]
        out.append(n)
        if n["level"] in UNIT_LEVELS or n["level"] in ("CHAPTER", "SCHEDULE", "DOCUMENT"):
            break
        k = n.get("parent")
    return out


def _schedule_of(idx, key: str) -> Optional[dict]:
    k = key
    while k and k in idx.nodes:
        if idx.nodes[k]["level"] == "SCHEDULE":
            return idx.nodes[k]
        k = idx.nodes[k].get("parent")
    return None


def _sibling_rank(idx, node: dict) -> int:
    parent = node.get("parent")
    same = [c for c in idx.children.get(parent, []) if idx.nodes[c]["level"] == node["level"]]
    return same.index(node["element"]) + 1 if node["element"] in same else 1


def describe(idx, key: str) -> str:
    """'clause (b) of sub-section (1) of section 7' / 'the first proviso to sub-section (2) of section 16'."""
    words = []
    for n in _chain(idx, key):
        lv, no = n["level"], _num(n)
        if lv == "SECTION":
            words.append(("of", f"section {no}"))
        elif lv == "RULE":
            words.append(("of", f"rule {no}"))
        elif lv == "ENTRY":
            sch = _schedule_of(idx, n["element"])
            words.append(("of", f"paragraph {no} of Schedule {sch['number'] if sch else ''}".strip()))
        elif lv == "PARAGRAPH":
            words.append(("of", f"paragraph {no}"))
        elif lv in ("SUB_SECTION",):
            words.append(("of", f"sub-section ({no})"))
        elif lv == "SUB_RULE":
            words.append(("of", f"sub-rule ({no})"))
        elif lv == "CLAUSE":
            words.append(("of", f"clause ({no})"))
        elif lv == "SUB_CLAUSE":
            words.append(("of", f"sub-clause ({no})"))
        elif lv in ("ITEM", "SUB_ITEM"):
            words.append(("of", f"item ({no})"))
        elif lv == "SUB_PARAGRAPH":
            words.append(("of", f"sub-paragraph {no}"))
        elif lv == "PROVISO":
            r = _sibling_rank(idx, n)
            words.append(("to", f"the {ORDINAL.get(r, str(r) + 'th')} proviso"))
        elif lv == "EXPLANATION":
            words.append(("to", "Explanation" + (f" {no}" if no and no != "1" or _sibling_rank(idx, n) > 1 else "")))
        elif lv == "ILLUSTRATION":
            words.append(("to", "the Illustration"))
        elif lv == "CLOSING":
            words.append(("of", "the closing words"))
        elif lv == "TABLE_ROW":
            words.append(("of", f"serial number {no}"))
        elif lv == "TABLE":
            words.append(("in", "the Table"))
        elif lv == "SUBJECT":
            words.append(("of", "the subject"))
        elif lv in ("CHAPTER",):
            words.append(("of", f"Chapter {no}"))
    if not words:
        return key
    text = words[0][1]
    for (conn, _), (_, w) in zip(words, words[1:]):
        text += f" {conn} {w}"
    return text


def citation(idx, key: str) -> str:
    """'Section 7(1)(a)', 'Section 16(2), first proviso', 'Schedule II, paragraph 5(b)', 'Rule 7, Table, serial number 2'."""
    chain = list(reversed(_chain(idx, key)))
    head, brackets, tail = "", "", []
    for n in chain:
        lv, no = n["level"], _num(n)
        if lv == "SECTION":
            head = f"Section {no}"
        elif lv == "RULE":
            head = f"Rule {no}"
        elif lv == "ENTRY":
            sch = _schedule_of(idx, n["element"])
            head = f"Schedule {sch['number'] if sch else ''}, paragraph {no}"
        elif lv == "PARAGRAPH":
            head = f"Paragraph {no}"
        elif lv == "CHAPTER":
            head = f"Chapter {no}"
        elif lv in LIST_LEVELS:
            if tail:
                tail.append(f"({no})")
            else:
                brackets += f"({no})"
        elif lv == "PROVISO":
            tail.append(f"{ORDINAL.get(_sibling_rank(idx, n), '')} proviso".strip())
        elif lv == "EXPLANATION":
            tail.append("Explanation" + (f" {no}" if _sibling_rank(idx, n) > 1 or (no and no != "1") else ""))
        elif lv == "ILLUSTRATION":
            tail.append("Illustration")
        elif lv == "TABLE":
            tail.append("Table")
        elif lv == "TABLE_ROW":
            tail.append(f"serial number {no}")
        elif lv == "SUBJECT":
            head = "Subject"
    return ", ".join([head + brackets] + tail) if head else key


def iso_date(text: str) -> str:
    t = (text or "").strip().lower().replace(",", " ")
    m = re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?([a-z]+)\s+(\d{4})", t)
    if m and m.group(2) in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return "NA"


def _heading_phrase(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip(" .:-—–")
    if not t:
        return ""
    if t.isupper():
        return t.lower()
    return t[0].lower() + t[1:] if not (len(t) > 1 and t[1].isupper()) else t


# ---------------------------------------------------------------- the drafter

class Drafter:
    def __init__(self, idx, doc_type: str, doc_name: str):
        self.idx = idx
        self.doc_type = doc_type.upper()
        self.doc_name = doc_name

    # -- context
    def unit_of(self, key: str) -> Optional[dict]:
        k = key
        while k and k in self.idx.nodes:
            if self.idx.nodes[k]["level"] in UNIT_LEVELS or self.idx.nodes[k]["level"] == "SUBJECT":
                return self.idx.nodes[k]
            k = self.idx.nodes[k].get("parent")
        return None

    def container_of(self, key: str) -> Optional[dict]:
        k = self.idx.nodes[key].get("parent") if key in self.idx.nodes else None
        while k and k in self.idx.nodes:
            if self.idx.nodes[k]["level"] in ("CHAPTER", "SCHEDULE", "PART", "ANNEX"):
                return self.idx.nodes[k]
            k = self.idx.nodes[k].get("parent")
        return None

    def topic(self, key: str) -> str:
        unit = self.unit_of(key)
        if unit is None:
            cont = self.container_of(key) or self.idx.nodes.get(key, {})
            return plain(cont.get("own_text", "")) or cont.get("heading", "")
        if unit["level"] == "SUBJECT":
            return strip_label(unit)
        if unit["level"] == "ENTRY":
            sch = _schedule_of(self.idx, unit["element"])
            return f"SCHEDULE {sch['number']} - {sch.get('heading', '')}".strip(" -") if sch else unit.get("heading", "")
        if unit["level"] == "PARAGRAPH":
            subj = next((n for n in self.idx.nodes.values() if n["level"] == "SUBJECT"), None)
            return strip_label(subj) if subj else f"Paragraph {unit['number']}"
        return f"{unit['number']}. {unit.get('heading') or ''}".strip(". ")

    def heading_path(self, key: str) -> List[str]:
        path = []
        cont = self.container_of(key)
        if cont:
            path.append(cont.get("heading") or plain(cont.get("own_text", "")))
        unit = self.unit_of(key)
        if unit and unit.get("heading"):
            path.append(unit["heading"])
        return [p for p in path if p]

    def pageno(self, node: dict) -> str:
        a, b = node.get("page_start"), node.get("page_end")
        return str(a) if a == b or not b else f"{a}-{b}"

    def xrefs(self, keys: List[str]) -> List[str]:
        out = []
        for k in keys:
            for x in self.idx.nodes.get(k, {}).get("xrefs") or []:
                cue = re.sub(r"\s+", " ", x.get("cue", "")).strip()
                if cue and cue not in out:
                    out.append(cue)
        return out

    # -- structure
    def has_list_children(self, key: str) -> bool:
        return any(self.idx.nodes[c]["level"] in LIST_LEVELS + ("TABLE",) for c in self.idx.children.get(key, []))

    def lead_ins(self, key: str) -> Tuple[List[str], List[str]]:
        """Lead-in texts from the nearest ancestor up to the sentence start (unit / proviso / closing ...)."""
        texts, keys = [], []
        k = self.idx.nodes[key].get("parent")
        while k and k in self.idx.nodes:
            n = self.idx.nodes[k]
            if n["level"] in ("CHAPTER", "SCHEDULE", "DOCUMENT", "PART", "ANNEX"):
                break
            if n["level"] != "TABLE":
                body = current_text(strip_label(n))
                if body:
                    texts.append(_lead_in(body))
                    keys.append(k)
            if n["level"] in UNIT_LEVELS or n["level"] in SENTENCE_STARTERS:
                break
            k = n.get("parent")
        return list(reversed(texts)), list(reversed(keys))

    def closing_for(self, key: str) -> Tuple[str, str]:
        """Closing words of the parent that come after this item ('shall be treated neither as ...')."""
        node = self.idx.nodes[key]
        parent = node.get("parent")
        kids = self.idx.children.get(parent, [])
        if key not in kids:
            return "", ""
        for c in kids[kids.index(key) + 1:]:
            if self.idx.nodes[c]["level"] == "CLOSING":
                return _lead_in(current_text(plain(self.idx.nodes[c]["own_text"]))), c
        return "", ""

    def framable(self, key: str) -> bool:
        n = self.idx.nodes[key]
        if n["level"] == "SUBJECT":
            return True
        if n["level"] in NOT_FRAMED or n.get("role") in ("NON_OPERATIVE", "HISTORICAL"):
            return False
        if self.has_list_children(key):
            return False
        return n["status"] != "OMITTED" and bool(current_text(strip_label(n)))

    # -- MKPs
    def text_mkp(self, key: str) -> DraftMKP:
        n = self.idx.nodes[key]
        own = current_text(strip_label(n))
        parts, src = [], [key]
        group = n.get("parent") or key
        if n["level"] in LIST_LEVELS or n["level"] == "TABLE_ROW":
            leads, lead_keys = self.lead_ins(key)
            closing, closing_key = self.closing_for(key)
            parts = leads + [_item_end(own, bool(closing))] + ([closing] if closing else [])
            src = lead_keys + [key] + ([closing_key] if closing_key else [])
        elif n["level"] in SENTENCE_STARTERS or n["level"] in UNIT_LEVELS or n["level"] == "SUBJECT":
            parts = [own if n["level"] != "SUBJECT" else plain(n["own_text"])]
            group = key if n["level"] in UNIT_LEVELS else group
        else:
            parts = [own]
        text = sentence(parts)
        parent = n.get("parent") if n["level"] not in UNIT_LEVELS and n["level"] != "SUBJECT" else "NA"
        if parent and parent in self.idx.nodes and self.idx.nodes[parent]["level"] in ("CHAPTER", "SCHEDULE", "DOCUMENT"):
            parent = "NA"
        return DraftMKP(f"{key}#TEXT", key, "TEXT", "METADATA" if n["level"] == "SUBJECT" else "TEXT", text,
                        citation(self.idx, key), key, n["level"], parent or "NA", self.topic(key), self.heading_path(key),
                        self.pageno(n), "NA", self.xrefs(src), group,
                        fixed_knowledge_type="METADATA" if n["level"] == "SUBJECT" else "", source_keys=src)

    def heading_mkp(self, key: str) -> Optional[DraftMKP]:
        n = self.idx.nodes[key]
        lv = n["level"]
        if lv == "CHAPTER":
            title = n.get("heading") or ""
            text = f"Chapter {n['number']} deals with {_heading_phrase(title)}." if title else ""
            sub = "Chapter heading"
        elif lv == "SCHEDULE":
            title = re.sub(r"^\[?\s*See [^\]]*\]\s*", "", n.get("heading") or "")
            text = f"Schedule {n['number']} deals with {_heading_phrase(title)}." if title else ""
            sub = "Schedule heading"
        elif lv in ("SECTION", "RULE"):
            title = n.get("heading") or ""
            text = f"{lv.title()} {n['number']} deals with {_heading_phrase(title)}." if title else ""
            sub = f"{lv.title()} heading"
        elif lv == "ENTRY":
            title = n.get("heading") or ""
            sch = _schedule_of(self.idx, key)
            text = (f"Paragraph {n['number']} of Schedule {sch['number'] if sch else ''} deals with {_heading_phrase(title)}."
                    if title and self.has_list_children(key) else "")
            sub = "Paragraph heading"
        else:
            return None
        if not text:
            return None
        return DraftMKP(f"{key}#HEAD", key, "HEAD", "HEADING", re.sub(r"\s+", " ", text), citation(self.idx, key) + " heading",
                        key, lv, "NA", self.topic(key) if lv not in ("CHAPTER", "SCHEDULE") else plain(n["own_text"]),
                        self.heading_path(key), self.pageno(n), "NA", [], key,
                        fixed_knowledge_type="HEADING", fixed_subtopic=sub, fixed_phrases=[re.sub(r"\s+", " ", text)],
                        source_keys=[key])

    def amendment_mkps(self, key: str) -> List[DraftMKP]:
        n = self.idx.nodes[key]
        amends = n.get("amendments") or []
        if not amends:
            return []
        raw = plain(n.get("own_text", ""))
        segments = list(re.finditer(r"\[\s*(\*+)\s*\]|\[([^\[\]]+)\]", raw))
        desc = describe(self.idx, key)
        out = []
        whole = bool(segments) and len(segments) == 1 and (segments[0].end() - segments[0].start()) > 0.7 * len(raw)
        for i, a in enumerate(amends, 1):
            fn = a.get("footnote", "")
            seg = segments[i - 1] if len(segments) == len(amends) else None
            if n["status"] == "OMITTED" and re.search(r"\*", raw):
                stars = re.search(r"\[\s*\*+\s*\]", raw)
                t = f"{desc[0].upper() + desc[1:]} is omitted, as shown by {stars.group(0) if stars else '[*****]'} with footnote {fn}."
            elif seg is not None and seg.group(1):
                t = f"In {desc}, text is omitted, as shown by {seg.group(0)} with footnote {fn}."
            elif seg is not None and not whole:
                inner = re.sub(r"\s+", " ", seg.group(2)).strip()
                t = f'In {desc}, the text "{inner}" is shown in square brackets with footnote {fn}.'
            else:
                t = f"{desc[0].upper() + desc[1:]} is shown in square brackets with footnote {fn}."
            from ikp_ss.layout import normalise_text
            note = re.sub(r"\s+", " ", normalise_text(a.get("text", ""))).strip()
            if note:
                t += f" Footnote {fn} states: {note.rstrip('.')}."
            refs = [x for x in (a.get("instrument"), f"Footnote {fn}") if x]
            out.append(DraftMKP(f"{key}#AMD{i}", key, f"AMD{i}", "AMENDMENT", t.replace('"', '"'), citation(self.idx, key),
                                key, n["level"], "NA", self.topic(key), self.heading_path(key), self.pageno(n),
                                iso_date(a.get("effective", "")), refs, f"{key}#AMD",
                                fixed_knowledge_type="AMENDMENT", fixed_subtopic="Amendment history", fixed_phrases=[t],
                                source_keys=[key]))
        return out

    def draft(self, root: str, with_unit_heading: bool = True, with_container_heading: bool = False) -> List[DraftMKP]:
        """Every MKP for the sub-tree under `root`, in source order."""
        idx = self.idx
        keys = [root] + idx.descendants(root)
        out: List[DraftMKP] = []
        if with_container_heading:
            cont = self.container_of(root)
            if cont is not None:
                h = self.heading_mkp(cont["element"])
                if h:
                    out.append(h)
        for k in keys:
            n = idx.nodes[k]
            if n.get("role") in ("NON_OPERATIVE", "HISTORICAL") and n["level"] != "SUBJECT":
                continue
            if k == root or n["level"] in UNIT_LEVELS:
                if with_unit_heading or k != root:
                    h = self.heading_mkp(k)
                    if h:
                        out.append(h)
            if self.framable(k):
                out.append(self.text_mkp(k))
            out.extend(self.amendment_mkps(k))
        return out
