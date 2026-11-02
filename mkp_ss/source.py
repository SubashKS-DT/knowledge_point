"""MKP / the runtime input for one legal unit, built from the IKP index (no PDF re-search).

One batch = one unit (section / rule / schedule paragraph / circular paragraph) or a part of it.
The cluster of the old prompts is now that unit: C_<unit key>.

  provisions  every node the models must see: PROVISION / HEADING / LEAD_IN / CLOSING, with source text,
              [[PAGE n]] markers and footnote text (exact text from the IKP index)
  expected    the PROVISION and HEADING keys - every one must get an MKP (old gate rule)
  references  the Python-built MKP text per element (draft.py) - used only to CHECK the models
"""
from __future__ import annotations

import json
from typing import Dict, List

from .draft import Drafter
from .scope import label_of

ROLE_ORDER = ("HEADING", "PROVISION", "LEAD_IN", "CLOSING")


def _footnotes(node: dict) -> str:
    out = []
    for a in node.get("amendments") or []:
        n, t = a.get("footnote"), (a.get("text") or "").strip()
        if n or t:
            out.append(f"{n}: {t}" if n else t)
    return " | ".join(out)


def unit_context(drafter: Drafter, batch, doc: dict, only_keys=None) -> dict:
    """only_keys: build the context for these MKPs only (the ones not already saved) - the models then see only
    their provisions (with lead-ins / closing text), so a re-run never pays again for finished MKPs."""
    idx = drafter.idx
    drafts = drafter.draft(batch.root, batch.with_unit_heading, batch.with_container_heading)
    if only_keys is not None:
        drafts = [d for d in drafts if d.mkp_key in only_keys]
    unit = drafter.unit_of(batch.root)
    unit_key = unit["element"] if unit else batch.root
    roles: Dict[str, set] = {}
    only_amd: Dict[str, bool] = {}

    def add(k, r):
        roles.setdefault(k, set()).add(r)

    for d in drafts:
        if d.kind == "HEADING":
            add(d.node_key, "HEADING")
            only_amd[d.node_key] = False
        elif d.kind == "AMENDMENT":
            only_amd.setdefault(d.node_key, True)
        else:
            add(d.node_key, "PROVISION")
            only_amd[d.node_key] = False
            for k in d.source_keys:
                if k != d.node_key and k in idx.nodes:
                    add(k, "CLOSING" if idx.nodes[k]["level"] == "CLOSING" else "LEAD_IN")
    shared = []
    for k, amd_only in only_amd.items():
        if not amd_only:
            continue
        n = idx.nodes[k]
        omitted = n.get("status") == "OMITTED" or "[*" in (n.get("own_text") or "")
        if omitted:                      # omitted provision: its amendment MKP is its record (old gate rule)
            add(k, "PROVISION")
        else:                            # e.g. a lead-in with [text]n: amendment MKP only, not framed alone
            shared.append({"owner": k})
            if k not in roles:
                add(k, "LEAD_IN" if idx.children.get(k) else "PROVISION")
    position = {k: i for i, k in enumerate(idx.order)}
    keys = sorted(roles, key=lambda k: position.get(k, 10 ** 9))
    shared_keys = {s["owner"] for s in shared}
    expected = [k for k in keys if roles[k] & {"PROVISION", "HEADING"} and k not in shared_keys]
    provisions, pages = [], set()
    for k in keys:
        n = idx.nodes[k]
        a, b = n.get("page_start"), n.get("page_end") or n.get("page_start")
        if a:
            pages.update(range(int(a), int(b) + 1))
        provisions.append({
            "key": k, "role": " + ".join(r for r in ROLE_ORDER if r in roles[k]), "level": n["level"],
            "parent": n.get("parent") or "NA", "heading": n.get("heading") or "",
            "heading_path": drafter.heading_path(k), "status": n.get("status") or "",
            "footnotes": _footnotes(n), "source_text": n.get("own_text") or ""})
    members = [{"key": k, "role": "MEMBER"} for k in expected]
    card = {"id": f"C_{unit_key}", "name": label_of(idx, unit_key), "question": "NA", "members": members,
            "shared_amendments": shared, "unit": unit_key, "scope": batch.root}
    refs = {}
    for d in drafts:
        refs[(d.citation_key, "HEADING" if d.kind == "HEADING" else d.part)] = d
    return {"cluster": card, "members": members, "provisions": provisions, "expected": expected,
            "pages": sorted(pages), "legal_parents": {p["key"]: p["parent"] for p in provisions},
            "drafts": drafts, "references": refs, "unit_key": unit_key, "scope_key": batch.root,
            "label": batch.label, "doc": doc}


# ---------------------------------------------------------------- prompts (same layout as the old engine)

def _source_block(context: dict) -> str:
    blocks = []
    for p in context["provisions"]:
        blocks.append("\n".join([
            f"NODE KEY: {p['key']}", f"ROLE: {p['role']}", f"LEVEL: {p['level']}",
            f"PARENT: {p['parent']}", f"HEADING: {p['heading']}",
            f"HEADING PATH: {json.dumps(p['heading_path'], ensure_ascii=False)}",
            f"STATUS: {p['status']}", f"FOOTNOTES: {p['footnotes']}",
            "SOURCE TEXT:", p["source_text"]]))
    return "\n\n===== NEXT NODE =====\n\n".join(blocks)


def _card(context: dict) -> dict:
    c = context["cluster"]
    return {k: c[k] for k in ("id", "name", "question", "shared_amendments")}


def _header(context: dict) -> str:
    d = context["doc"]
    return (f"Instrument Name: {d['name']}\nInstrument Code: {d['code']}\nDocument Type: {d['type']}\n"
            f"Page Range: {','.join(map(str, context['pages'])) or 'NA'}")


def mkp_prompt(base_prompt: str, context: dict, registry: List[dict]) -> str:
    return f"""{base_prompt}

======================================================================
RUNTIME INPUT
======================================================================
CONTEXT HEADER
{_header(context)}

CLUSTER CARD
{json.dumps(_card(context), ensure_ascii=False)}

CLUSTER MEMBERS
{json.dumps(context['members'], ensure_ascii=False)}

CLUSTER PROVISIONS
{_source_block(context)}

EXPECTED PROVISION LIST
{json.dumps(context['expected'], ensure_ascii=False)}

MKP REGISTRY SLICE
{json.dumps(list(registry), ensure_ascii=False)}

Return only CLUSTER|TYPE|ELEMENT|RESULTS using the MKP prompt above.
"""


def judge_prompt(base_prompt: str, context: dict, registry: List[dict], gemini_raw: str, claude_raw: str,
                 pregate: str, previous_errors: List[str] = None) -> str:
    retry = ""
    if previous_errors:
        retry = ("\n===== YOUR PREVIOUS FINAL FAILED THE PYTHON GATE =====\n"
                 + "\n".join(f"- {e}" for e in previous_errors)
                 + "\nFix exactly these items; keep every other FINAL value as it was.\n")
    return f"""{base_prompt}

===== VERIFIED RUNTIME CONTEXT =====
{_header(context)}
CLUSTER CARD: {json.dumps(_card(context), ensure_ascii=False)}
CLUSTER MEMBERS: {json.dumps(context['members'], ensure_ascii=False)}
EXPECTED PROVISION LIST: {json.dumps(context['expected'], ensure_ascii=False)}

===== ORIGINAL PDF WORDING (PRIMARY AUTHORITY) =====
{_source_block(context)}

===== GEMINI MKP (MODEL_1) =====
{gemini_raw}

===== CLAUDE MKP (MODEL_2) =====
{claude_raw}

===== PRE-GATE REPORT =====
{pregate}

===== EXISTING MKP REGISTRY SLICE =====
{json.dumps(registry, ensure_ascii=False)}
{retry}
FINAL INSTRUCTION: Compare both candidate tables against ORIGINAL PDF WORDING above.
Always return the complete seven-column judge table exactly as defined in your Judge prompt.
When source text is incomplete or ambiguous, state the problem, do not fabricate a provision.
"""
