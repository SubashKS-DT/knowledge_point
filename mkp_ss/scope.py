"""MKP / scope selection and batches.

Interactive (mkp.ask_user_input = true):
    1 Full document | 2 Chapter / Schedule | 3 Type a citation (section 16(2)(c), rule 36(4)) | 4 Type a key
    then drill down: Chapter -> Section/Rule -> Sub-section -> Clause, "0" = take the whole of the current level.
From permission.json (ask_user_input = false):
    "scope": "FULL"   or   ["Chapter III", "section 7", "rule 55(1)", "CGSTA17_SCHII"]
A scope becomes BATCHES: one batch per section / rule / schedule entry / circular paragraph,
or one batch for a smaller selected piece (sub-section, clause ...).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from .draft import LIST_LEVELS, UNIT_LEVELS, plain

CONTAINERS = ("DOCUMENT", "CHAPTER", "SCHEDULE", "PART")


@dataclass
class Batch:
    key: str                 # batch id (unit key or selected sub-tree key)
    label: str
    root: str
    with_unit_heading: bool = True
    with_container_heading: bool = False
    cluster_id: str = ""            # C_ id from IKP cluster card (empty if not available)
    business_question: str = ""     # pass to the MKP prompt as the cluster's decision question


def label_of(idx, key: str) -> str:
    n = idx.nodes[key]
    lv, no = n["level"], n.get("number") or ""
    head = (n.get("heading") or "").strip()
    if lv == "CHAPTER":
        return f"Chapter {no} - {head}"
    if lv == "SCHEDULE":
        return f"Schedule {no} - {head[:60]}"
    if lv in ("SECTION", "RULE"):
        return f"{lv.title()} {no} - {head}"
    if lv == "ENTRY":
        return f"Paragraph {no} - {head[:60]}"
    if lv == "PARAGRAPH":
        return f"Paragraph {no}"
    if lv == "SUBJECT":
        return "Subject"
    if lv == "DOCUMENT":
        return "Full document"
    text = plain(n.get("own_text", ""))
    return f"{lv.replace('_', ' ').title()} ({no}) {text[:70]}" if lv in LIST_LEVELS else f"{lv.title()} {no} {text[:60]}"


def operative(idx, key: str) -> bool:
    return idx.nodes[key].get("role") not in ("NON_OPERATIVE", "HISTORICAL") or idx.nodes[key]["level"] == "SUBJECT"


def selectable_children(idx, key: str) -> List[str]:
    out = []
    for c in idx.children.get(key, []):
        n = idx.nodes[c]
        if not operative(idx, c):
            continue
        if n["level"] in CONTAINERS or n["level"] in UNIT_LEVELS or n["level"] in LIST_LEVELS or n["level"] in (
                "PROVISO", "EXPLANATION", "SUBJECT", "TABLE_ROW"):
            out.append(c)
    return out


def resolve_token(idx, token: str) -> str:
    t = token.strip()
    if t.upper() in ("FULL", "ALL", "WHOLE", "FULL PDF"):
        return idx.doc
    if t in idx.nodes:
        return t
    if f"{idx.doc}_{t}" in idx.nodes:
        return f"{idx.doc}_{t}"
    m = re.match(r"(?i)^chapter\s+([IVXLC]+)$", t)
    if m and f"{idx.doc}_CH{m.group(1).upper()}" in idx.nodes:
        return f"{idx.doc}_CH{m.group(1).upper()}"
    m = re.match(r"(?i)^schedule\s+([IVXLC]+)$", t)
    if m and f"{idx.doc}_SCH{m.group(1).upper()}" in idx.nodes:
        return f"{idx.doc}_SCH{m.group(1).upper()}"
    return idx.resolve(t)


def ask_scope(idx, ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> str:
    """Interactive menu; returns the selected root key."""
    def pick(prompt: str, n: int, allow_zero: bool = False) -> int:
        while True:
            v = ask(prompt).strip()
            if v.isdigit() and (allow_zero and int(v) == 0 or 1 <= int(v) <= n):
                return int(v)
            show("  Enter one of the numbers shown.")
    show(f"\nMKP SCOPE - {idx.doc}")
    show("  1. Full document\n  2. Choose chapter / schedule (then section, sub-section, clause)\n"
         "  3. Type a citation  (e.g. section 16(2)(c), rule 36(4), Schedule II para 5(b))\n  4. Type a IKP key")
    choice = pick("Select: ", 4)
    if choice == 1:
        return idx.doc
    if choice in (3, 4):
        while True:
            try:
                return resolve_token(idx, ask("Citation / key: "))
            except Exception as exc:
                show(f"  Not found: {exc}")
    current = idx.doc
    while True:
        kids = selectable_children(idx, current)
        if not kids:
            return current
        show(f"\n{label_of(idx, current)}")
        if current != idx.doc:
            show("  0. Take the whole of this")
        for i, k in enumerate(kids, 1):
            show(f"  {i}. {label_of(idx, k)}")
        v = pick("Select: ", len(kids), allow_zero=current != idx.doc)
        if v == 0:
            return current
        current = kids[v - 1]


def batches_for(idx, root: str) -> List[Batch]:
    """Split a selected root into processing batches.

    When idx carries a cluster index (loaded from DB), each Batch is annotated
    with the cluster_id and business_question from IKP, per CLAUDE.md §4.
    """
    n = idx.nodes[root]
    if n["level"] in CONTAINERS or n["level"] in ("FRONT",):
        out, seen_containers = [], set()
        for k in [root] + idx.descendants(root):
            node = idx.nodes[k]
            if (node["level"] in UNIT_LEVELS or node["level"] == "SUBJECT") and operative(idx, k):
                parent = idx.nodes.get(node.get("parent"), {})
                if parent.get("level") in UNIT_LEVELS:
                    continue  # paragraph inside an annex order etc.
                cont = _container(idx, k)
                first = cont is not None and cont not in seen_containers and cont != idx.doc
                if cont is not None:
                    seen_containers.add(cont)
                out.append(Batch(k, label_of(idx, k), k, True, first))
        _annotate_cluster_ids(idx, out)
        return out
    if n["level"] in UNIT_LEVELS or n["level"] == "SUBJECT":
        batches = [Batch(root, label_of(idx, root), root, True, False)]
        _annotate_cluster_ids(idx, batches)
        return batches
    return [Batch(root, label_of(idx, root), root, False, False)]


def _annotate_cluster_ids(idx, batches: List[Batch]) -> None:
    """Attach IKP cluster_id and business_question to each batch, if available."""
    get_clusters = getattr(idx, "get_clusters_for", None)
    if not get_clusters:
        return
    keys = [b.key for b in batches]
    cards = get_clusters(keys)
    card_by_member: dict = {}
    for card in cards:
        for mk in card.get("members", []):
            card_by_member[mk] = card
    for batch in batches:
        card = card_by_member.get(batch.key)
        if card:
            batch.cluster_id = card["cluster_id"]
            batch.business_question = card.get("business_question", "")


def _container(idx, key: str) -> Optional[str]:
    k = idx.nodes[key].get("parent")
    while k and k in idx.nodes:
        if idx.nodes[k]["level"] in ("CHAPTER", "SCHEDULE", "PART"):
            return k
        k = idx.nodes[k].get("parent")
    return None


def complexity(drafts, idx, settings: dict) -> tuple:
    """SIMPLE / COMPLEX tier from the batch's own structure."""
    text_chars = sum(len(d.text) for d in drafts)
    provisos = sum(1 for d in drafts if d.level in ("PROVISO", "EXPLANATION", "ILLUSTRATION"))
    tables = sum(1 for d in drafts if d.level == "TABLE_ROW")
    amends = sum(1 for d in drafts if d.kind == "AMENDMENT")
    score = len(drafts) + text_chars / 1500 + 2 * provisos + 3 * tables + amends
    tier = "COMPLEX" if score >= settings["complex_threshold"] or tables else "SIMPLE"
    return tier, round(score, 1)
