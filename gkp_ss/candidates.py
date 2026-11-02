"""GKP / candidate subgroups for one atom. Candidates only - the LLMs decide membership.

Sources (each candidate records why it was proposed):
  concept / atom   pgvector cosine of the atom's concept_embedding and atom_embedding vs. the subgroup embedding
  term             a subgroup include-term appears in the atom's statement / concept / family
  anchor           the atom's provision, or a provision it references, is a key provision of the subgroup
  fact             other atoms testing the same fact were approved into the subgroup (the fact network)
  reference        atoms of a provision this atom references were approved into the subgroup
  section          other atoms of the same section were approved into the subgroup
  similar          the nearest atoms (by meaning) of OTHER documents were approved into the subgroup - how a
                   notification or circular reaches the TSG of the Act / Rule it works with, without anchors
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from psycopg2 import sql

from transport import get_field

STOP = {"goods", "service", "services", "supply", "form", "forms", "legal", "other", "general", "person", "tax", "gst",
        "and", "the", "with", "without", "transaction", "transactions", "section", "rule", "act"}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9()' -]", " ", str(t or "").lower())).strip()


def _field_list(results: str, name: str) -> List[str]:
    m = re.search(rf"\b{name}:\s*\[([^\]]*)\]", results or "")
    return [x.strip().strip('"') for x in m.group(1).split(",") if x.strip()] if m else []


class CandidateFinder:
    def __init__(self, store, by_id: Dict[str, dict], settings: dict, taxonomy_table: str = "gkp_taxonomy"):
        self.store, self.by_id, self.s = store, by_id, settings
        self.T = store.t(taxonomy_table)
        self.terms = {sid: [t for t in (_norm(x) for x in sub["include"]) if len(t) >= 4 and t not in STOP]
                      for sid, sub in by_id.items()}

    def _vector(self, vec: Optional[str], groups: set, k: int) -> List[tuple]:
        if not vec:
            return []
        try:
            return self.store.run(sql.SQL("""SELECT tsg_id, 1 - (embedding <=> %s::vector) FROM {}
                WHERE tg_id = ANY(%s) AND COALESCE(active, true) AND vector_dims(embedding) = vector_dims(%s::vector)
                ORDER BY embedding <=> %s::vector LIMIT %s""").format(self.T), (vec, sorted(groups), vec, vec, k), fetch=True)
        except Exception:
            self.store.conn.rollback()
            return []

    def find(self, atom: dict, groups: set, k: int, exclude: set = frozenset(), full: bool = True) -> List[dict]:
        found: Dict[str, dict] = {}

        def add(sid, source, concept=None, atom_score=None, similar=None):
            if sid not in self.by_id or self.by_id[sid]["tg_id"] not in groups or sid in exclude:
                return
            c = found.setdefault(sid, {"tsg_id": sid, "concept_score": None, "atom_score": None, "sources": []})
            if similar is not None:
                c["_similar"] = max(similar, c.get("_similar") or -1)
            if concept is not None:
                c["concept_score"] = max(concept, c["concept_score"] or -1)
            if atom_score is not None:
                c["atom_score"] = max(atom_score, c["atom_score"] or -1)
            if source not in c["sources"]:
                c["sources"].append(source)

        for sid, sim in self._vector(atom.get("concept_vector"), groups, k):
            add(sid, f"concept {sim:.2f}", concept=float(sim))
        for sid, sim in self._vector(atom.get("atom_vector"), groups, k):
            add(sid, f"atom {sim:.2f}", atom_score=float(sim))
        inst = (atom.get("base") or {}).get("instrument") or atom.get("instrument") or ""
        if self.s.get("similar_atoms", 0) and atom.get("atom_vector"):
            for sid, sim, aid in self.store.subgroups_by_similar_atom(atom["atom_vector"], inst, self.s["similar_atoms"],
                                                                     self.s["similar_atom_min"]):
                add(sid, f"similar {aid} {float(sim):.2f}", similar=float(sim))
        if full:
            results = atom.get("results") or ""
            text = " " + _norm(" ".join([get_field({"RESULTS": results}, "statement"), atom.get("concept") or "",
                                         atom.get("family") or ""])) + " "
            for sid, terms in self.terms.items():
                hit = next((t for t in terms if f" {t} " in text), None)
                if hit:
                    add(sid, f"term '{hit}'")
            prov = atom["source_mkp"].split("#", 1)[0]
            refs = _field_list(results, "reference_keys")
            # the most specific anchor wins: CGSTA17_SCHII_EN1 beats the broad CGSTA17_SCHII, which then counts
            # only as a weak hint ("broad anchor", no rank bonus) instead of pulling in every Schedule II entry
            hits = []
            for sid, sub in self.by_id.items():
                for a in sub.get("anchors") or []:
                    if prov == a or prov.startswith(a + "_"):
                        hits.append((sid, a, "own"))
                    elif any(r == a or r.startswith(a + "_") for r in refs):
                        hits.append((sid, a, "ref"))
            for kind in ("own", "ref"):
                mine = [(sid, a) for sid, a, k in hits if k == kind]
                longest = max((len(a) for _, a in mine), default=0)
                for sid, a in mine:
                    label = "anchor" if kind == "own" else "anchor via reference"
                    add(sid, f"{label} {a}" if len(a) == longest else f"broad {label} {a}")
            for f in atom.get("facts") or []:
                for sid, n in self.store.subgroups_by_fact(f, atom["source_mkp"]):
                    add(sid, f"fact {f} ({n} atoms)")
            for r in refs:
                for m in self.store.approved_for_provision(r):
                    add(m["tsg_id"], f"reference {r}")
            from .store import unit_prefix
            for m in self.store.approved_for_provision(unit_prefix(atom["source_mkp"])):
                add(m["tsg_id"], "section neighbour")
        static_src = ("concept", "atom", "term", "anchor", "similar")
        static, dynamic = [], []
        for sid, c in found.items():
            sub = self.by_id[sid]
            best = max(c["concept_score"] or 0, c["atom_score"] or 0, c.get("_similar") or 0)
            fixed = [x for x in c["sources"] if x.startswith(("term", "anchor"))]
            rank = best + 0.08 * len(fixed)
            c = {k: v for k, v in c.items() if k != "_similar"}
            item = dict(c, rank=round(rank, 4), tg_id=sub["tg_id"], group=sub["tg_name"], subgroup=sub["name"],
                        definition=sub["definition"], include=sub["include"], exclude=sub["exclude"],
                        key_provisions=sub.get("key_provisions") or [])
            (static if any(x.startswith(static_src) for x in c["sources"]) else dynamic).append(item)
        # stable part: embeddings / terms / anchors only (does not change when other MKPs get grouped)
        static.sort(key=lambda c: (-c["rank"], c["tsg_id"]))
        # network part: subgroups reached only through shared facts, references or section neighbours
        dynamic.sort(key=lambda c: (-len(c["sources"]), c["tsg_id"]))
        return static[: self.s["max_candidates"]] + dynamic[: max(3, self.s["max_candidates"] // 3)]
