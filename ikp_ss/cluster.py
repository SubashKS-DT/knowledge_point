"""IKP / Step 5 — deterministic cluster extraction.

Per CLAUDE.md §3: every PROVISION must be in a cluster (C_ row) or listed as UNC.
Per CLAUDE.md §1: a cluster maps to a decision-graph Step with a business question.

Rules:
  - One cluster per top-level SECTION / RULE within a CHAPTER or PART.
  - One cluster per ENTRY within a SCHEDULE (each entry is an independent provision).
  - One cluster per PARAGRAPH / SUBJECT in circulars and notifications.
  - All descendants of the cluster root (sub-sections, clauses, provisos, etc.) are members.
  - Non-operative and HISTORICAL nodes are skipped from membership.
  - Provisions with no parent cluster root are returned as UNC.

The business question is Python-derived from the heading; the LLM assist (ikp.llm) may
refine it when the chapter quality gate fails.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

CLUSTERABLE_ROOTS = {"SECTION", "RULE", "ENTRY", "PARAGRAPH", "SUBJECT"}
NON_OPERATIVE_ROLES = {"NON_OPERATIVE", "HISTORICAL"}
CONTAINER_LEVELS = {"DOCUMENT", "FRONT", "CHAPTER", "SCHEDULE", "PART", "ANNEX", "BODY"}


# ------------------------------------------------------------------ ID helpers

_STOP = frozenset({"of", "and", "or", "in", "on", "to", "the", "a", "an", "for",
                   "be", "is", "are", "by", "at", "as", "its", "their"})


def _acronym(text: str, max_len: int = 8) -> str:
    words = re.findall(r"[A-Za-z]+", text)
    tokens = [w for w in words if w.lower() not in _STOP] or words
    return "".join(t[0].upper() for t in tokens[:max_len])


def derive_cluster_id(name: str) -> str:
    """Stable cluster ID from a heading or section name.

    Matches the helper in ikp_engine.py so IDs are consistent.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", str(name or ""))
    if not tokens:
        return "C_UNKNOWN"
    return "C_" + "".join(t if t.isdigit() else t[0].upper() for t in tokens)[:10]


def _unique_id(heading: str, level: str, number: str, used: Set[str]) -> str:
    base = _acronym(heading) if heading.strip() else (level[0].upper() + re.sub(r"[^A-Z0-9]", "", number.upper()))
    base = base or "X"
    cid = f"C_{base}"
    if cid not in used:
        used.add(cid)
        return cid
    n = 2
    while f"{cid}{n}" in used:
        n += 1
    result = f"{cid}{n}"
    used.add(result)
    return result


# ------------------------------------------------------------------ business question

def _business_question(level: str, number: str, heading: str) -> str:
    h = heading.strip()
    if h:
        clean = re.sub(r"[^A-Za-z0-9 ]+", "", h).strip()
        # Convert "Scope of supply" → "Is 'scope of supply' applicable?"
        return f"Is '{clean.lower()}' applicable to this transaction?"
    return f"Does {level.lower()} {number} apply?"


# ------------------------------------------------------------------ core

@dataclass
class ClusterCard:
    cluster_id: str
    business_question: str
    root_key: str
    member_keys: List[str] = field(default_factory=list)
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    origin: str = "PYTHON"


def derive_clusters(doc, group) -> Tuple[List[ClusterCard], List[str]]:
    """Return (cluster_cards, unclustered_provision_keys) for one chapter group.

    doc  — ParsedDocument from ikp_ss.structure
    group — ChapterGroup from ikp_ss.build
    """
    nodes = doc.nodes
    keys_in_group: Set[str] = set(group.keys)
    used_ids: Set[str] = set()
    cards: List[ClusterCard] = []
    assigned: Dict[str, str] = {}  # provision_key -> cluster_id

    for key in group.keys:
        node = nodes[key]
        if node.level not in CLUSTERABLE_ROOTS:
            continue
        if node.role in NON_OPERATIVE_ROLES:
            continue

        # Only top-level roots: parent must be a container, not another clusterable unit
        parent = nodes.get(node.parent) if node.parent and node.parent in nodes else None
        if parent and parent.level not in CONTAINER_LEVELS:
            continue  # nested unit; will be a member of the parent's cluster

        cid = _unique_id(node.heading or "", node.level, node.number or "", used_ids)
        bq = _business_question(node.level, node.number or "", node.heading or "")
        pages = doc.pages_of(node.start, node.end) if node.start >= 0 else []

        card = ClusterCard(
            cluster_id=cid,
            business_question=bq,
            root_key=key,
            member_keys=[key],
            page_from=pages[0] if pages else None,
            page_to=pages[-1] if pages else None,
        )
        assigned[key] = cid

        # Add all descendants that are in this group and are operative, in doc order
        _collect_members(doc, node.children, keys_in_group, card, assigned, nodes)

        cards.append(card)

    # Provisions with no assigned cluster → UNC
    unc = [
        k for k in group.keys
        if nodes[k].level in CLUSTERABLE_ROOTS
        and nodes[k].role not in NON_OPERATIVE_ROLES
        and k not in assigned
    ]
    return cards, unc


def _collect_members(doc, children, keys_in_group, card, assigned, nodes):
    """Recursively collect descendants into a cluster card (DFS, document order)."""
    for key in children:
        if key not in keys_in_group or key not in nodes:
            continue
        node = nodes[key]
        if node.role not in NON_OPERATIVE_ROLES and key not in assigned:
            card.member_keys.append(key)
            assigned[key] = card.cluster_id
        _collect_members(doc, node.children, keys_in_group, card, assigned, nodes)


# ------------------------------------------------------------------ row builders

def _val(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().replace(";", ",")


def _results(fields: dict) -> str:
    return "; ".join(f"{k}: {_val(v)}" for k, v in fields.items() if v not in (None, "", []))


def to_rows(doc_code: str, cards: List[ClusterCard], unc_keys: List[str]) -> List[dict]:
    """Return C_, CM_, and UNC index rows ready for group_records()."""
    rows: List[dict] = []

    for card in cards:
        page_str = (f"{card.page_from}-{card.page_to}"
                    if card.page_from and card.page_to else "")
        rows.append({
            "DOC": doc_code,
            "TYPE": "C",
            "ELEMENT": card.cluster_id,
            "PAGE_NUMBER": page_str,
            "RESULTS": _results({
                "root": card.root_key,
                "business_question": card.business_question,
                "members": ",".join(card.member_keys),
                "member_count": len(card.member_keys),
                "origin": card.origin,
            }),
        })
        for i, mk in enumerate(card.member_keys, 1):
            rows.append({
                "DOC": doc_code,
                "TYPE": "CM",
                "ELEMENT": mk,
                "PAGE_NUMBER": "",
                "RESULTS": _results({
                    "cluster": card.cluster_id,
                    "pathway_order": i,
                }),
            })

    for k in unc_keys:
        rows.append({
            "DOC": doc_code,
            "TYPE": "UNC",
            "ELEMENT": k,
            "PAGE_NUMBER": "",
            "RESULTS": "reason: no cluster match",
        })

    return rows
