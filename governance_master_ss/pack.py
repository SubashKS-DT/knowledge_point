"""GOVERNANCE MASTER / governance master: turn a built governance master into readable table rows, check it without the engine, and say what
changed since the last time it was written.
"""
from __future__ import annotations

import json
from typing import Dict, List

from . import rules as R

SECTION_OF = {"facts": "FACT", "predicates": "CHECK", "groups": "GROUP", "outcomes": "OUTCOME",
              "overrides": "OVERRIDE", "triggers": "TRIGGER", "products": "PRODUCT"}


def check_pack(pack: dict) -> List[str]:
    """Everything the engine would refuse, found in Python: unknown ids, bad rules, dangling steps.
    An empty list means the governance master is consistent."""
    errs: List[str] = []
    facts = set(pack.get("facts", {}))
    nodes = set(pack.get("predicates", {})) | set(pack.get("groups", {})) | set(pack.get("products", {}))
    outcomes = set(pack.get("outcomes", {}))
    overrides = set(pack.get("overrides", {}))
    for pid, p in pack.get("predicates", {}).items():
        if p.get("order") not in ("LOW", "HIGH"):
            errs.append(f"check {pid}: order must be LOW or HIGH, not {p.get('order')!r}")
        elif p.get("order") == "LOW":
            errs += [f"check {pid}: {e}" for e in (R.check(p.get("rule", ""), facts) if p.get("rule") else ["no rule"])]
        else:
            if not p.get("interpretation"):
                errs.append(f"check {pid}: a judged check needs its question")
            errs += [f"check {pid}: unknown fact {f}" for f in p.get("grounding", []) if f not in facts]
    for gid, g in pack.get("groups", {}).items():
        if g.get("logic") not in ("ALL", "ANY"):
            errs.append(f"group {gid}: logic must be ALL or ANY")
        errs += [f"group {gid}: unknown member {m}" for m in g.get("members", []) if m not in nodes]
    for oid, o in list(pack.get("outcomes", {}).items()) + list(pack.get("overrides", {}).items()):
        errs += [f"outcome {oid}: {e}" for e in R.check(o.get("expr", ""), set(), nodes)]
    for tid, t in pack.get("triggers", {}).items():
        errs += [f"review flag {tid}: {e}" for e in R.check(t.get("trigger", ""), facts)]
        if t.get("beside") not in outcomes:
            errs.append(f"review flag {tid}: beside unknown outcome {t.get('beside')}")
    produced, owned = set(), set()
    for c in pack.get("compounds", []):
        for o in c.get("includes", []):
            if o not in outcomes:
                errs.append(f"step {c['id']}: unknown outcome {o}")
            if o in owned:
                errs.append(f"step {c['id']}: outcome {o} is in two steps")
            owned.add(o)
        for o in c.get("overrides", []):
            if o not in overrides:
                errs.append(f"step {c['id']}: unknown override {o}")
        for pr in c.get("produces", []):
            if pr["product"] not in pack.get("products", {}):
                errs.append(f"step {c['id']}: unknown product {pr['product']}")
            produced.add(pr["product"])
        for o in c.get("exclusive", []):
            if o not in c.get("includes", []):
                errs.append(f"step {c['id']}: exclusive outcome {o} is not in the step")
    used_products = {t for o in pack.get("outcomes", {}).values() for t in R.ID_TOKEN.findall(o.get("expr", ""))
                     if t.startswith("PD_")}
    errs += [f"product {p} is used but no step produces it" for p in sorted(used_products - produced)]
    for pr in pack.get("precedents", []):
        if pr.get("predicate") not in pack.get("predicates", {}):
            errs.append(f"precedent {pr.get('id')}: unknown check {pr.get('predicate')}")
    return errs


def rows(pack: dict, report: dict, id_map: List[dict], spine: dict, names: dict, status: str) -> List[dict]:
    """One row per element of the governance master, for the main table governance_master."""
    by_id = {m["rulebook_id"]: m for m in id_map}
    why = {x["mkp"]: x["reason"] for x in report.get("included", [])}
    spine_ids = set()
    for sec in SECTION_OF:
        spine_ids |= set(spine.get(sec, {}))
    spine_ids |= {c["id"] for c in spine.get("compounds", [])}
    steps = pack.get("compounds", [])
    step_of_outcome = {o: (i + 1, c["id"]) for i, c in enumerate(steps) for o in c.get("includes", []) + c.get("overrides", [])}
    step_of_product = {p["product"]: (i + 1, c["id"]) for i, c in enumerate(steps) for p in c.get("produces", [])}
    step_of_mkp = {}
    for i, c in enumerate(steps):
        m = by_id.get(c["id"])
        if m:
            step_of_mkp[m["source_mkp"]] = (i + 1, c["id"])
    pos = {c["id"]: i + 1 for i, c in enumerate(steps)}
    for mkp, cid in (report.get("step_of_mkp") or {}).items():      # clauses merged into one step
        if cid in pos:
            step_of_mkp[mkp] = (pos[cid], cid)
    base = {"tsg_id": names["tsg"], "tsg_name": names["tsg_name"], "tg_id": names["tg"], "tg_name": names["tg_name"],
            "pack_status": status}

    def src(eid: str) -> dict:
        if eid in spine_ids and eid not in by_id:
            return {"source_mkp": "SPINE", "mkp_id": None, "akp_id": None, "citation": "CGST Act s.7 (spine)",
                    "included_as": "SPINE"}
        m = by_id.get(eid)
        if not m:
            return {"source_mkp": None, "mkp_id": None, "akp_id": None, "citation": None, "included_as": None}
        return {"source_mkp": m["source_mkp"], "mkp_id": m["mkp_id"], "akp_id": m["akp_id"] or None,
                "citation": m["source_mkp"].split("#", 1)[0], "included_as": why.get(m["source_mkp"], "")}

    out = [dict(base, section="HEADER", step_no=None, step_id=None, element_id=names["tsg"], element_type="GOVERNANCE_MASTER",
                text=f"{names['tsg']} {names['tsg_name']}", logic=f"{len(steps)} steps",
                check_order=None, facts=None, source_mkp=None, mkp_id=None, akp_id=None, citation=None,
                included_as=None, attributes={"laws": [x["mkp"] for x in report.get("included", [])],
                                              "counts": report.get("counts", {}), "warnings": report.get("warnings", []),
                                              "blocking": report.get("blocking", [])})]
    for i, c in enumerate(steps):
        out.append(dict(base, section="STEP", step_no=i + 1, step_id=c["id"], element_id=c["id"],
                        element_type="GATE" if c.get("gate") else "STEP", text=c.get("question"),
                        logic="includes " + ", ".join(c.get("includes", [])), check_order=None, facts=None,
                        attributes=c, **src(c["id"])))
    for sec, label in SECTION_OF.items():
        for eid, e in pack.get(sec, {}).items():
            step = step_of_outcome.get(eid) or step_of_product.get(eid)
            s = src(eid)
            if not step and s.get("source_mkp") in step_of_mkp:
                step = step_of_mkp[s["source_mkp"]]
            if label == "FACT":
                text, logic, order, facts = e.get("name"), e.get("type") + (
                    f" [{', '.join(e['options'])}]" if e.get("options") else ""), e.get("tier"), None
                s = {"source_mkp": "SPINE" if eid in spine.get("facts", {}) else None, "mkp_id": None, "akp_id": None,
                     "citation": None, "included_as": "SPINE" if eid in spine.get("facts", {}) else "FACT"}
            elif label == "CHECK":
                text = e.get("statement")
                logic = e.get("rule") or e.get("interpretation")
                order = e.get("order")
                facts = ", ".join(sorted(R.parse(e["rule"])["facts"])) if e.get("rule") else ", ".join(e.get("grounding", []))
            elif label == "GROUP":
                text, logic, order, facts = e.get("description") or e.get("name"), \
                    f"{e.get('logic')} [{', '.join(e.get('members', []))}]", None, None
            elif label in ("OUTCOME", "OVERRIDE"):
                text, logic, order, facts = e.get("description") or e.get("outcome"), e.get("expr"), None, None
            elif label == "TRIGGER":
                text, logic, order = e.get("question"), e.get("trigger"), None
                facts = ", ".join(sorted(R.parse(e["trigger"])["facts"])) if e.get("trigger") else None
            else:
                text, logic, order, facts = e.get("statement"), None, None, None
            out.append(dict(base, section=label, step_no=step[0] if step else None, step_id=step[1] if step else None,
                            element_id=eid, element_type=label, text=text, logic=logic, check_order=order,
                            facts=facts, attributes=e, **s))
    for p in pack.get("precedents", []):
        out.append(dict(base, section="PRECEDENT", step_no=None, step_id=None, element_id=p["id"],
                        element_type="PRECEDENT", text=p.get("statement"), logic=p.get("when"), check_order=None,
                        facts=", ".join(p.get("decisive_facts", [])), attributes=p, **src(p["id"])))
    return out


def change(old: Dict[tuple, str], new_rows: List[dict]) -> dict:
    """What changed in the governance master: element ids added, changed and removed (HEADER row ignored)."""
    new = {(r["section"], r["element_id"]): json.dumps(r["attributes"], sort_keys=True, ensure_ascii=False, default=str)
           for r in new_rows if r["section"] not in ("HEADER", "PACK")}
    old = {k: json.dumps(json.loads(v), sort_keys=True, ensure_ascii=False) for k, v in old.items() if k[0] not in ("HEADER", "PACK")}
    return {"added": sorted(f"{s}:{e}" for (s, e) in new.keys() - old.keys()),
            "removed": sorted(f"{s}:{e}" for (s, e) in old.keys() - new.keys()),
            "changed": sorted(f"{s}:{e}" for (s, e) in new.keys() & old.keys() if new[(s, e)] != old[(s, e)])}
