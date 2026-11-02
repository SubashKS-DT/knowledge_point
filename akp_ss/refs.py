"""AKP / references - Python only, no prompt.

For every atom, every law it refers to ("as per Schedule I", "sub-section (1)", "section 16(2)", "rule 36(4)",
"FORM GST REG-01", "Notification No. 12/2017") goes into RESULTS as
    reference: [Schedule I, sub-section (1)]; reference_keys: [CGSTA17_SCHI, CGSTA17_S7_SS1]
The key comes from the IKP index: its cross-references (xrefs) already know where each phrase of the source
points; absolute citations are resolved with IkpIndex.resolve. A reference the atom makes that the source MKP
does not make is reported (an invented reference is an error).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

ABSOLUTE = re.compile(
    r"\b(?:section|rule)\s+\d+[A-Z]{0,3}(?:\s*\(\s*\w{1,6}\s*\))*"
    r"|\bschedule\s+(?:[IVX]+|\d+)\b"
    r"|\bchapter\s+[IVXL]+[A-Z]?\b"
    r"|\bform\s+gst\s+[A-Z]+(?:\s*[-–]\s*\d+[A-Z]?)?"
    r"|\bnotification\s+no\.?\s*[\w/.-]+",
    re.I)
ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}


def _norm(t: str) -> str:
    t = str(t or "").lower().replace("–", "-").replace("―", '"').replace("‖", '"')
    t = re.sub(r"\s*\(\s*", "(", t)
    t = re.sub(r"\s*\)\s*", ") ", t)
    return re.sub(r"\s+", " ", t).strip()


def _label(span: str) -> str:
    s = re.sub(r"\s+", " ", span.strip())
    m = re.fullmatch(r"(?i)schedule\s+(\d+)", s)
    if m and int(m.group(1)) in ROMAN:              # 'Schedule 1' -> 'Schedule I'
        return f"Schedule {ROMAN[int(m.group(1))]}"
    return s[0].upper() + s[1:] if s else s


class RefFinder:
    def __init__(self, conn, instrument: str, schema: str = "public", prefix: str = "ikp"):
        self.idx = None
        try:
            from ikp_ss.reader import IkpIndex
            self.idx = IkpIndex.from_db(conn, instrument, schema, prefix)
        except Exception:
            conn.rollback()
            self.idx = None             # no IKP index -> literal references only, no keys

    def source_refs(self, citation_key: str) -> List[Tuple[str, Optional[str]]]:
        """(cue, target key) of the provision, its lead-ins and its closing line (from IKP xrefs)."""
        if not self.idx or citation_key not in self.idx.nodes:
            return []
        keys = [citation_key] + list(self.idx.ancestors(citation_key))
        parent = self.idx.nodes[citation_key].get("parent")
        for k in self.idx.children.get(parent, []) if parent else []:
            if self.idx.nodes[k]["level"] in ("CLOSING",):
                keys.append(k)
        out = []
        for k in keys:
            n = self.idx.nodes.get(k) or {}
            if n.get("level") in ("DOCUMENT", "CHAPTER", "PART"):
                continue
            for x in n.get("xrefs") or []:
                cue = (x.get("cue") or "").strip()
                if cue:
                    out.append((cue, x.get("to") if x.get("resolved", True) else None))
        return out

    def _resolve(self, span: str) -> Optional[str]:
        if not self.idx:
            return None
        try:
            return self.idx.resolve(_label(span))
        except Exception:
            return None

    def for_atom(self, statement: str, citation_key: str, source_text: str) -> Tuple[List[str], List[str], List[str]]:
        """-> (reference labels, reference keys, invented references)"""
        st, src = _norm(statement), _norm(source_text)
        labels: Dict[str, Optional[str]] = {}
        src_keys = set()
        for cue, key in self.source_refs(citation_key):
            if key:
                src_keys.add(key)
            if _norm(cue) and re.search(r"(?<![\w])" + re.escape(_norm(cue)) + r"(?![\w(])", st):
                labels.setdefault(_label(cue), key)
        invented = []
        for m in ABSOLUTE.finditer(statement or ""):
            span = m.group(0)
            key = self._resolve(span)
            if any(_norm(span) in _norm(l) or (key and key == k) for l, k in labels.items()):
                continue
            if _norm(span) in src or _norm(_label(span)) in src or (key and key in src_keys):
                labels.setdefault(_label(span), key)
            else:
                invented.append(_label(span))
        # drop a shorter label already inside a longer one ('section 16' inside 'section 16(2)')
        names = list(labels)
        keep = [n for n in names if not any(n != o and _norm(n) in _norm(o) for o in names)]
        return keep, [labels[n] for n in keep if labels[n]], invented


def set_references(row: dict, labels: List[str], keys: List[str]) -> None:
    """Replace any model-written reference field with the Python list."""
    r = re.sub(r";?\s*\breference(?:_keys)?:\s*\[[^\]]*\]", "", row["RESULTS"]).strip(" ;")
    if labels:
        r += "; reference: [" + ", ".join(labels) + "]"
    if keys:
        r += "; reference_keys: [" + ", ".join(dict.fromkeys(keys)) + "]"
    row["RESULTS"] = r
