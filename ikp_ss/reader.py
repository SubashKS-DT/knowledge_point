"""IKP index reader - the one interface MKP (and anything else) uses to get law text.

    idx = IkpIndex.load("CGSTA17", paths, settings)        # latest version from DB (or JSONL)
    idx.get("CGSTA17_S7_SS1_Ca")                             # node record (text, pages, blocks, anchors ...)
    idx.resolve("section 7(1)(a)")                           # -> "CGSTA17_S7_SS1_Ca"
    idx.context("CGSTA17_S7_SS1_Ca")                         # node + parent lead-ins + closing + provisos
    idx.read_from_pdf("Law/CGST-Act.pdf", "CGSTA17_S7_SS1_Ca")   # cut the block out of the PDF again
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .citations import convention_key, parse_citation, walk


def _letters(text: str) -> str:
    from .layout import normalise_text
    return re.sub(r"[^a-z]", "", normalise_text(text).lower())


def _strip_pages(text: str) -> str:
    return re.sub(r"\[\[PAGE \d+\]\]\n?", "", text or "")


class IkpIndex:
    def __init__(self, doc: str, nodes: List[dict], manifest: Optional[dict] = None,
                 index_rows: Optional[List[dict]] = None):
        self.doc = doc
        self.nodes: Dict[str, dict] = {n["element"]: n for n in nodes}
        self.order = [n["element"] for n in sorted(nodes, key=lambda n: n.get("ord") or 0)]
        self.manifest = manifest or {}
        self.children: Dict[str, List[str]] = {}
        for k in self.order:
            n = self.nodes[k]
            kids = n.get("children")
            if isinstance(kids, str):
                kids = json.loads(kids)
            self.children[k] = list(kids or [])
        self._tree = {k: {"level": n["level"], "number": n.get("number") or ""} for k, n in self.nodes.items()}
        # Cluster index: built from C/CM rows in the index table
        self._cluster_cards: Dict[str, dict] = {}  # cluster_id -> card dict
        self._cm_index: Dict[str, str] = {}          # provision_key -> cluster_id
        if index_rows:
            self._build_cluster_index(index_rows)

    def _build_cluster_index(self, index_rows: List[dict]) -> None:
        """Parse C and CM rows from the index table into lookup structures."""
        def _parse_results(results: str) -> dict:
            out: dict = {}
            for seg in str(results or "").split(";"):
                if ":" not in seg:
                    continue
                k, v = seg.split(":", 1)
                out[k.strip()] = v.strip()
            return out

        raw_cards: dict = {}
        for row in index_rows:
            rtype = str(row.get("TYPE") or row.get("type") or "").upper()
            element = str(row.get("ELEMENT") or row.get("element") or "")
            results = str(row.get("RESULTS") or row.get("results") or "")
            if rtype == "C":
                fields = _parse_results(results)
                members_str = fields.get("members", "")
                members = [m.strip() for m in members_str.split(",") if m.strip()]
                raw_cards[element] = {
                    "cluster_id": element,
                    "business_question": fields.get("business_question", ""),
                    "root": fields.get("root", ""),
                    "members": members,
                    "member_count": int(fields.get("member_count", len(members))),
                    "origin": fields.get("origin", "PYTHON"),
                }
            elif rtype == "CM":
                fields = _parse_results(results)
                cid = fields.get("cluster", "")
                if cid:
                    self._cm_index[element] = cid

        self._cluster_cards = raw_cards

    # ------------------------------------------------------------ loading
    @classmethod
    def from_jsonl(cls, path) -> "IkpIndex":
        with Path(path).open(encoding="utf-8") as fh:
            manifest = json.loads(fh.readline())["_manifest"]
            nodes = [json.loads(line) for line in fh]
        # C/CM rows are not stored in the JSONL (it stores node records only).
        # Cluster index will be empty until a DB load is used.
        return cls(manifest["doc"], nodes, manifest)

    @classmethod
    def from_db(cls, conn, doc: str, schema: str = "public", prefix: str = "ikp",
                version: Optional[int] = None) -> "IkpIndex":
        from .store import Store
        st = Store(conn, schema, prefix)
        nodes = st.load_nodes(doc, version)
        if not nodes:
            raise LookupError(f"No IKP index saved for {doc}" + (f" version {version}" if version else ""))
        index_rows = st.load_rows(doc, version)
        return cls(doc, nodes, {"version_no": nodes[0]["version_no"]}, index_rows=index_rows)

    @classmethod
    def load(cls, doc: str, paths: dict, settings: dict, version: Optional[int] = None) -> "IkpIndex":
        """DB first (latest or given version), JSONL fallback."""
        if settings.get("save_to_db", True):
            try:
                from ikp_engine import get_connection
                conn = get_connection(paths)
                try:
                    return cls.from_db(conn, doc, settings.get("schema", "public"), settings.get("table_prefix", "ikp"), version)
                finally:
                    conn.close()
            except LookupError:
                raise
            except Exception as exc:
                from kp_console import log
                log(f"IKP READER : database unavailable ({type(exc).__name__}); using JSONL")
        base = Path(settings.get("jsonl_dir", "Output/ikp"))
        if not base.is_absolute():
            base = Path(__file__).resolve().parents[1] / base
        return cls.from_jsonl(base / f"{doc}.ikp.jsonl")

    # ------------------------------------------------------------ Index API (CLAUDE.md §3)

    def get_node(self, key: str) -> dict:
        """Alias for get(key) — canonical CLAUDE.md §3 name."""
        return self.get(key)

    def get_subtree(self, key: str) -> List[str]:
        """Return all descendant keys in DFS order (alias for descendants())."""
        return self.descendants(key)

    def find(self, instrument: str, chapter: Optional[str] = None,
             section: Optional[str] = None, sub_section: Optional[str] = None,
             clause: Optional[str] = None) -> str:
        """Structured lookup by instrument + level numbers (CLAUDE.md §3 Index API).

        Each argument is the printed number/identifier, e.g.
            find("CGSTA17", chapter="III", section="7", sub_section="1", clause="a")
        Returns the most specific key that can be resolved.  Raises KeyError if
        nothing matches.
        """
        # Build a citation string and delegate to resolve()
        parts = []
        if section:
            parts.append(f"section {section}")
            if sub_section:
                parts.append(f"({sub_section})")
            if clause:
                parts.append(f"({clause})")
        elif chapter:
            # Chapter-level lookup: return the chapter key directly
            key = f"{instrument}_CH{chapter.upper()}"
            if key in self.nodes:
                return key
            raise KeyError(f"Chapter {chapter} not found in {instrument} index")
        if not parts:
            raise ValueError("find() requires at least one of: chapter, section")
        return self.resolve(" ".join(parts))

    def get_clusters_for(self, keys: List[str]) -> List[dict]:
        """Return cluster cards for the clusters that contain any of the given provision keys.

        Returns a list of card dicts (cluster_id, business_question, members, root, origin),
        deduplicated and in the order the clusters were first encountered.
        Returns [] when the cluster index was not loaded (JSONL path without DB).
        """
        seen: set = set()
        cards: List[dict] = []
        for key in keys:
            cid = self._cm_index.get(key)
            if cid and cid not in seen:
                seen.add(cid)
                card = self._cluster_cards.get(cid)
                if card:
                    cards.append(card)
        return cards

    def get_cluster_card(self, c_id: str) -> dict:
        """Return the cluster card dict for a given cluster ID."""
        cards = getattr(self, "_cluster_cards", {})
        if c_id not in cards:
            raise KeyError(f"Cluster {c_id!r} not found in the IKP index of {self.doc}")
        return cards[c_id]

    # ------------------------------------------------------------ lookup
    def get(self, key: str) -> dict:
        if key not in self.nodes:
            raise KeyError(f"{key} is not in the IKP index of {self.doc}")
        return self.nodes[key]

    def text(self, key: str, with_pages: bool = True) -> str:
        t = self.get(key)["own_text"] or ""
        return t if with_pages else _strip_pages(t).strip()

    def ancestors(self, key: str) -> List[str]:
        out, seen = [], set()
        parent = self.get(key).get("parent")
        while parent and parent in self.nodes and parent not in seen:
            out.append(parent)
            seen.add(parent)
            parent = self.nodes[parent].get("parent")
        return out

    def descendants(self, key: str) -> List[str]:
        out, stack = [], list(reversed(self.children.get(key, [])))
        while stack:
            k = stack.pop()
            out.append(k)
            stack.extend(reversed(self.children.get(k, [])))
        return out

    def resolve(self, citation: str) -> str:
        """'section 16(2)(c)' / 'rule 36(4)' / 'Schedule II para 5(b)' / '7(1)(a)' -> element key."""
        parsed = parse_citation(citation)
        if not parsed:
            raise ValueError(f"Cannot read citation: {citation!r}")
        kind, unit, chain = parsed
        if kind == "section" and re.match(r"^[A-Z]+R\d", self.doc):
            kind = "rule"  # bare '36(4)' against the Rules
        unit_key = convention_key(self.doc, kind, unit, [])
        if unit_key not in self.nodes:
            raise KeyError(f"{citation!r}: {unit_key} not in the index")
        key, ok = walk(self._tree, self.children, unit_key, [("chain", t) for t in chain])
        if not ok:
            raise KeyError(f"{citation!r}: resolved only down to {key}")
        return key

    def context(self, key: str) -> dict:
        """Everything MKP needs to frame one provision without opening the PDF."""
        node = self.get(key)
        lead_ins = [{"element": a, "level": self.nodes[a]["level"], "text": self.text(a, False)}
                    for a in reversed(self.ancestors(key)) if self.nodes[a]["level"] not in ("DOCUMENT",)]
        siblings_after = []
        parent = node.get("parent")
        if parent in self.nodes:
            kids = self.children.get(parent, [])
            for k in kids[kids.index(key) + 1:] if key in kids else []:
                if self.nodes[k]["level"] in ("CLOSING", "PROVISO", "EXPLANATION"):
                    siblings_after.append({"element": k, "level": self.nodes[k]["level"], "text": self.text(k, False)})
        return {"element": key, "level": node["level"], "status": node["status"], "pages": [node["page_start"], node["page_end"]],
                "text": self.text(key, False), "lead_ins": lead_ins, "qualifiers_after": siblings_after,
                "children": [{"element": c, "level": self.nodes[c]["level"], "text": self.text(c, False)}
                             for c in self.descendants(key)],
                "amendments": node.get("amendments") or [], "xrefs": node.get("xrefs") or [],
                "blocks": node.get("blocks") or []}

    # ------------------------------------------------------------ back to the PDF
    def read_from_pdf(self, pdf_path, key: str) -> dict:
        """Cut the node's exact block out of the PDF using its page boxes + anchors."""
        import pymupdf as fitz
        node = self.get(key)
        blocks = node.get("blocks") or []
        if isinstance(blocks, str):
            blocks = json.loads(blocks)
        if not blocks:
            return {"element": key, "text": "", "matches_index": False, "reason": "node has no PDF block"}
        with fitz.open(pdf_path) as pdf:
            pieces = []
            for b in blocks:
                x0, y0, x1, y1 = b["bbox"]
                rect = fitz.Rect(x0 - 3, y0 - 1, x1 + 3, y1 + 1)
                pieces.append(pdf.load_page(b["page"] - 1).get_text("text", clip=rect, sort=True))
        raw = "\n".join(pieces)
        stored = _strip_pages(node["own_text"]).strip()
        if not _letters(stored):
            # label-only node ('(c)' whose text is its sub-clauses, '(5) [*****]' omitted): compare the label
            label = re.sub(r"[\d\s]", "", stored)
            same = label.lstrip("[(") in re.sub(r"[\d\s]", "", raw)
            return {"element": key, "pages": [b["page"] for b in blocks], "text": stored, "matches_index": same,
                    "reason": "" if same else "label not found in PDF block"}
        hay, pos = [], []
        for i, ch in enumerate(raw):
            for c in _letters(ch):
                hay.append(c)
                pos.append(i)
        hay = "".join(hay)
        start_n, end_n = _letters(node.get("start_anchor") or ""), _letters(node.get("end_anchor") or "")
        s = hay.find(start_n) if start_n else 0
        expected = s + len(_letters(stored)) - len(end_n)  # where the end anchor should begin
        e = hay.find(end_n, max(s, expected - 30)) if end_n else len(hay) - 1
        if e < 0:
            e = hay.rfind(end_n)
        if s < 0 or e < 0 or e + len(end_n) - 1 < s:
            return {"element": key, "text": raw.strip(), "matches_index": False, "reason": "anchors not found in block"}
        cut = raw[pos[s]:pos[min(e + len(end_n), len(pos)) - 1] + 1] if pos else ""
        same = _letters(cut) == _letters(_strip_pages(node["own_text"]))
        return {"element": key, "pages": [b["page"] for b in blocks], "text": cut.strip(), "matches_index": same,
                "reason": "" if same else "PDF block differs from stored text (footnote digits / table order)"}
