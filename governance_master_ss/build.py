"""GOVERNANCE MASTER / build: one taxonomy sub-group -> one Decision Graph Engine rulebook (pure Python, no DB, no LLM).

    members      MKPs with an APPROVED GKP atom in the sub-group
  + links        what those laws need: products (PD) they consume, provisions they refer to, provisions that
                 override them, definitions of terms their atoms use (followed up to `prerequisite_depth`)
  - spine        MKPs already in the spine (s.7 supply test, Schedule II / III) are not converted again
  = rulebook     spine compounds first, then one compound per included MKP in dependency order

Conversion of AKP rows (see docs/PASS4_RULEBOOK.md):
  FACTS -> facts (type map, tier, scope)   A -> predicates (LOW rule / HIGH interpretation + grounding)
  G -> groups   LE -> outcomes + one product each   OR.exclusive -> compound.exclusive
  LK OVERRIDES / OVERRIDDEN_BY -> overrides   CT -> triggers   TC -> test transactions (not in the rulebook)
IDs from different MKPs may clash (two sections can both have A_SALE); clashing ids get the MKP tag as a
suffix and every rename is kept in the id map, so each node still traces back to its akp_id.
Nothing is guessed: whatever cannot be converted is left out and listed in the report with the reason.
"""
from __future__ import annotations

import copy
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from transport import parse_results

from . import rules as R
from .source import Snapshot

KEY_RX = re.compile(r"\b[A-Z][A-Z0-9]*\d{2}_[A-Z0-9_]+\b")       # IKP keys such as CGSTA17_S7_SS1_Ca
OPERATIVE_SKIP = {"HEADING", "AMENDMENT", "METADATA", "ILLUSTRATION", "OBITER"}


def rf(row: dict, key: str) -> str:
    return str(parse_results(row.get("results") or "").get(key.lower(), "") or "").strip()


def rlist(row: dict, key: str) -> List[str]:
    v = rf(row, key).strip().strip("[]")
    return [x.strip().strip("'\"") for x in v.split(",") if x.strip().strip("'\"") and x.strip().upper() != "NA"]


@dataclass
class Bundle:
    key: str                     # source_mkp, e.g. CGSTA17_S2_C31_SCa#TEXT
    mkp_id: str
    instrument: str
    rows: List[dict]
    citation_key: str = ""
    citation: str = ""
    knowledge_type: str = ""
    concept: str = ""
    nl: str = ""

    def of(self, kind: str) -> List[dict]:
        return [r for r in self.rows if r["type"] == kind]

    @property
    def tag(self) -> str:
        return re.sub(r"[^A-Z0-9]", "", (self.mkp_id or "").upper().removeprefix("M_")) or \
            re.sub(r"[^A-Z0-9]", "", self.citation_key.upper())[-10:]


@dataclass
class Built:
    rulebook: dict
    report: dict
    id_map: List[dict] = field(default_factory=list)
    tests: List[dict] = field(default_factory=list)
    included: List[str] = field(default_factory=list)


class GovernanceMasterError(RuntimeError):
    pass


def bundles_of(snap: Snapshot) -> "OrderedDict[str, Bundle]":
    out: "OrderedDict[str, Bundle]" = OrderedDict()
    for r in snap.akp:
        key = r["source_mkp"]
        b = out.get(key)
        if b is None:
            b = out[key] = Bundle(key, r.get("mkp_id") or "", r.get("instrument") or "", [])
        b.rows.append(r)
    for b in out.values():
        b.rows.sort(key=lambda r: int(r.get("ordinal") or 0))
        m = next((r for r in b.rows if r["type"] == "MKP"), None)
        if m:
            b.citation_key = rf(m, "citation_key") or b.key.split("#", 1)[0]
            b.citation = rf(m, "citation")
            b.knowledge_type = rf(m, "knowledge_type").upper()
            b.concept = rf(m, "concept") or (m.get("concept") or "")
            b.nl = rf(m, "nl")
        else:
            b.citation_key = b.key.split("#", 1)[0]
    return out


def _starts(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + "_")


# ---------------------------------------------------------------- 1. which MKPs go in

def select(snap: Snapshot, bundles: "OrderedDict[str, Bundle]", tsg: str, cfg: dict, spine: dict,
           depth: int) -> Tuple["OrderedDict[str, str]", dict]:
    """-> (MKP key -> reason, report part). Reasons: MEMBER, PRODUCT <pd>, REFERENCE <key>, OVERRIDE <key>,
    DEFINITION <term>."""
    covers = cfg["spine"].get("covers", [])
    follow = set(cfg.get("follow", []))
    report = {"members": [], "covered_by_spine": [], "added": [], "unresolved": [], "member_atoms": []}
    spine_products = set(spine.get("products", {}))
    p_alias = {k: v for k, v in cfg.get("product_aliases", {}).items() if not k.startswith("_")}

    produced_by: Dict[str, str] = {}
    for b in bundles.values():
        for le in b.of("LE"):
            for pd in rlist(le, "feeds") + ["PD_" + le["element"][3:]]:
                if pd.startswith("PD_"):
                    produced_by.setdefault(pd, b.key)

    def covered(b: Bundle) -> bool:
        return any(_starts(b.citation_key, c) for c in covers)

    chosen: "OrderedDict[str, str]" = OrderedDict()
    for g in snap.gkp:
        if g.get("tsg_id") == tsg and str(g.get("status", "")).upper() == "APPROVED":
            report["member_atoms"].append({"source_mkp": g["source_mkp"], "atom": g.get("akp_element"),
                                           "type": g.get("akp_type")})
            b = bundles.get(g["source_mkp"])
            if b is None:
                report["unresolved"].append(f"{g['source_mkp']}: grouped in GKP but no latest AKP rows")
                continue
            if covered(b):
                if b.key not in report["covered_by_spine"]:
                    report["covered_by_spine"].append(b.key)
                continue
            if b.key not in chosen:
                chosen[b.key] = "MEMBER"
                report["members"].append(b.key)
    if not chosen and not report["covered_by_spine"]:
        raise GovernanceMasterError(f"sub-group {tsg}: no APPROVED GKP atoms in the latest data")

    def add(key: str, why: str, queue: list, level: int) -> None:
        b = bundles[key]
        if covered(b):
            if key not in report["covered_by_spine"]:
                report["covered_by_spine"].append(key)
            return
        if key not in chosen:
            chosen[key] = why
            report["added"].append({"mkp": key, "reason": why})
            queue.append((key, level))

    queue = [(k, 0) for k in chosen]
    while queue:
        key, level = queue.pop(0)
        if level >= depth:
            continue
        b = bundles[key]
        if "products" in follow:
            for le in b.of("LE"):
                for pd in sorted(set(t for t in R.ID_TOKEN.findall(rf(le, "expr")) if t.startswith("PD_"))):
                    pd = p_alias.get(pd, pd)
                    if pd in spine_products:
                        continue
                    src = produced_by.get(pd)
                    if src and src != key:
                        add(src, f"PRODUCT {pd}", queue, level + 1)
                    elif not src:
                        report["unresolved"].append(f"{key}: product {pd} is produced by no loaded MKP")
        if "references" in follow:
            for a in b.of("A"):
                refs = rlist(a, "reference_keys") + KEY_RX.findall(rf(a, "reference"))
                for ref in dict.fromkeys(refs):
                    hits = [k for k, x in bundles.items() if _starts(x.citation_key, ref) and k != key]
                    if not hits and not any(_starts(ref, c) or _starts(c, ref) for c in covers):
                        report["unresolved"].append(f"{key}: reference {ref} is not loaded yet")
                    for h in hits:
                        add(h, f"REFERENCE {ref}", queue, level + 1)
        if "overrides" in follow:
            for lk in b.of("LK"):
                rel, target = rf(lk, "relation").upper(), rf(lk, "target")
                if rel == "OVERRIDDEN_BY" and target:
                    for h in [k for k, x in bundles.items() if _starts(x.citation_key, target)]:
                        add(h, f"OVERRIDE {target}", queue, level + 1)
            for k, x in bundles.items():
                for lk in x.of("LK"):
                    if rf(lk, "relation").upper() == "OVERRIDES" and _starts(b.citation_key, rf(lk, "target")) and k != key:
                        add(k, f"OVERRIDE {b.citation_key}", queue, level + 1)
        if "definitions" in follow and chosen.get(key) == "MEMBER":
            text = " ".join(rf(a, "statement") for a in b.of("A")).lower()
            for d in snap.definitions:
                term = str(d.get("term", "")).strip().strip('"').lower()
                where = d.get("defined_in", "")
                if len(term) > 3 and re.search(rf"\b{re.escape(term)}\b", text):
                    hits = [k for k, x in bundles.items() if _starts(x.citation_key, where) and k != key]
                    # the spine already gives this term (e.g. "supply" = s.7(1)): do not pull in the rest of its provision
                    if any(covered(bundles[h]) for h in hits) or any(_starts(c, where) or _starts(where, c) for c in covers if where):
                        continue
                    for h in hits:
                        add(h, f"DEFINITION {term}", queue, level + 1)
    return chosen, report


# ---------------------------------------------------------------- 2. law in force on a date

def in_force(snap: Snapshot, bundles, chosen, law_as_on: str) -> Dict[str, str]:
    """MKP key -> reason, for MKPs not in force on `law_as_on` (YYYY-MM-DD) according to the time facts."""
    out = {}
    if not law_as_on:
        return out
    for t in snap.time_facts:
        eff, kind, target = t.get("effective_from"), str(t.get("kind", "")).upper(), t.get("target_citation_key", "")
        if not eff or not target:
            continue
        for key in chosen:
            if not _starts(bundles[key].citation_key, target):
                continue
            if kind == "OMITTED" and eff <= law_as_on:
                out[key] = f"omitted with effect from {eff}"
            elif kind in ("INSERTED", "SUBSTITUTED") and eff > law_as_on:
                out[key] = f"{kind.lower()} with effect from {eff}, after {law_as_on}"
    return out


# ---------------------------------------------------------------- 3. conversion

def _clause_parent(citation_key: str) -> str:
    """CGSTR17_R27_Ca -> CGSTR17_R27 ; CGSTA17_S2_C31_SCb -> CGSTA17_S2_C31 ; '' when it is not a clause."""
    m = re.fullmatch(r"(.+?)_(?:C|SC|SSC)[a-z]{1,4}", citation_key or "")
    return m.group(1) if m else ""


def _strip_clause(citation: str) -> str:
    return re.sub(r"\([a-z]{1,4}\)$", "", str(citation or "").strip())


def value_aliases(rule: str, cfg: dict) -> str:
    """Config/governance_master_map.json value_aliases: AKP fact values that the spine asks in another way,
    e.g. F_SUPPLY_CONSIDERATION = 'NON_MONEY'  ->  F_NMC = TRUE (reviewed by people, never guessed)."""
    for item in cfg.get("value_aliases", []):
        if isinstance(item, dict) and item.get("pattern") and item.get("to"):
            rule = re.sub(item["pattern"], item["to"], rule)
    return rule


def _sentence(text: str) -> bool:
    return bool(text) and " " in text and not re.search(r"(=|!=|<|>|\bIN\s*\[|\bIS_SET\b|'[^']*')", text)


def build(snap: Snapshot, tsg: str, tsg_name: str, tg: str, tg_name: str, cfg: dict, spine: dict, version: str,
          depth: int = 3, law_as_on: str = "") -> Built:
    spine = copy.deepcopy(spine)
    bundles = bundles_of(snap)
    chosen, report = select(snap, bundles, tsg, cfg, spine, depth)
    report.update({"tsg": tsg, "tsg_name": tsg_name, "tg": tg, "tg_name": tg_name, "law_as_on": law_as_on or None,
                   "excluded": [], "warnings": [], "blocking": [], "facts": []})
    for key, why in in_force(snap, bundles, chosen, law_as_on).items():
        chosen.pop(key, None)
        report["excluded"].append({"mkp": key, "reason": f"not in force: {why}"})

    f_alias = {k: v for k, v in cfg.get("fact_aliases", {}).items() if not k.startswith("_")}
    p_alias = {k: v for k, v in cfg.get("product_aliases", {}).items() if not k.startswith("_")}
    type_map, type_warn = cfg["type_map"], cfg.get("type_warnings", {})
    spine_facts = spine.get("facts", {})
    taken: Set[str] = set()
    for section in ("facts", "predicates", "groups", "outcomes", "overrides", "triggers", "products"):
        taken |= set(spine.get(section, {}))
    taken |= {c["id"] for c in spine.get("compounds", [])} | {p["id"] for p in spine.get("precedents", [])}
    by_akp = {r.get("akp_id"): r for r in snap.akp if r.get("akp_id")}

    # ---- fact specs (registry first, then the FACTS rows written with the MKPs)
    fact_rows: Dict[str, dict] = {}
    for r in snap.akp:
        if r["type"] == "FACTS":
            fact_rows.setdefault(r["element"], r)

    def fact_spec(fid: str) -> Optional[dict]:
        reg, row = snap.facts.get(fid), fact_rows.get(fid)
        if not reg and not row:
            return None
        g = (lambda k: (reg or {}).get(k) or (rf(row, k) if row else ""))
        ptype = str((reg or {}).get("data_type") or (rf(row, "type") if row else "") or "TEXT").upper()
        etype = type_map.get(ptype, "TEXT")
        options = list(dict.fromkeys(str(o).strip() for o in ((reg or {}).get("options") or (rlist(row, "options") if row else []))
                                     if str(o).strip()))
        spec = {"name": (g("name") or fid)[:200], "type": etype}
        if ptype in type_warn:
            report["warnings"].append(f"{fid}: {type_warn[ptype]}")
        if etype == "CODE":
            if options:
                spec["options"] = options
            else:
                spec["type"] = "TEXT"
                report["warnings"].append(f"{fid}: CODE fact without options became TEXT")
        resolver = (rf(row, "resolver") if row else "").upper()
        tier_txt = (rf(row, "tier") if row else "").upper()
        evidence = " ".join(str(e) for e in ((reg or {}).get("evidence") or (rlist(row, "evidence") if row else []))).lower()
        t = cfg["tier"]
        if resolver in t["t1_resolvers"] or tier_txt in ("T1", "1"):
            tier = "T1"
        elif resolver in t["t0_resolvers"] or tier_txt in ("T0", "0"):
            tier = "T0"
        elif any(w in evidence for w in t["t0_evidence_words"]) and not any(w in evidence for w in t["t1_evidence_words"]):
            tier = "T0"
        elif any(w in evidence for w in t["t1_evidence_words"]):
            tier = "T1"
        else:
            tier = t["default"]
        spec["tier"] = tier
        if cfg.get("use_entity_scopes"):
            ent = str(g("entity") or "").upper()
            scope = cfg["scope_by_entity"].get(ent, cfg["scope_by_entity"]["_default"])
            if scope != "transaction":
                spec["scope"] = scope
        nl = str(g("nl") or "")
        if nl.strip().endswith("?"):
            spec["ask"] = nl.strip()
        report["facts"].append({"fact": fid, "akp_type": ptype, "engine_type": spec["type"], "tier": tier,
                                "evidence": evidence[:200], "derived_from": (reg or {}).get("derived_from") or []})
        return spec

    out_facts: "OrderedDict[str, dict]" = OrderedDict()
    known_facts = set(spine_facts) | set(snap.facts) | set(fact_rows)
    known_facts = {f_alias.get(f, f) for f in known_facts}

    def use_fact(fid: str) -> bool:
        fid = f_alias.get(fid, fid)
        if fid in spine_facts or fid in out_facts:
            return True
        original = next((k for k, v in f_alias.items() if v == fid), fid)
        spec = fact_spec(fid) or fact_spec(original)
        if spec is None:
            return False
        out_facts[fid] = spec
        return True

    # ---- per MKP: convert into a draft, then drop drafts that cannot stand
    drafts: "OrderedDict[str, dict]" = OrderedDict()
    id_map: List[dict] = []

    def new_id(old: str, b: Bundle, kind: str, akp_id: str = "") -> str:
        nid = old if re.fullmatch(r"[A-Z][A-Z0-9_]*", old or "") else re.sub(r"[^A-Z0-9_]", "_", (old or "X").upper())
        if nid in R.KEYWORDS:
            nid += "_X"
        if nid in taken:
            base, n = f"{nid}_{b.tag}", 2
            nid = base
            while nid in taken:
                nid, n = f"{base}{n}", n + 1
        taken.add(nid)
        id_map.append({"rulebook_id": nid, "akp_element": old, "kind": kind, "source_mkp": b.key,
                       "mkp_id": b.mkp_id, "akp_id": akp_id})
        return nid

    for key in list(chosen):
        b = bundles[key]
        if b.knowledge_type in OPERATIVE_SKIP or not b.of("LE"):
            report["excluded"].append({"mkp": key, "reason": f"no law expression ({b.knowledge_type or 'non-operative'})"})
            chosen.pop(key)
            continue
        d = {"bundle": b, "local": {}, "predicates": OrderedDict(), "groups": OrderedDict(), "outcomes": OrderedDict(),
             "products": OrderedDict(), "triggers": OrderedDict(), "exclusive": [], "needs": set(), "problems": []}
        local = d["local"]
        # ids first, so expressions can be renamed in any order
        for r in b.rows:
            if r["type"] in ("A", "A_REF", "G", "LE", "CT"):
                local[r["element"]] = new_id(r["element"], b, r["type"], r.get("akp_id") or "")
        for le in b.of("LE"):
            feeds = [p for p in rlist(le, "feeds") if p.startswith("PD_")]
            pid = feeds[0] if feeds else "PD_" + le["element"][3:]
            local.setdefault(pid, new_id(pid, b, "PD"))
            d["products"][local[pid]] = {"le": local[le["element"]], "akp": pid,
                                         "statement": (rf(le, "nl") or rf(le, "outcome") or le["element"])[:300]}
        drafts[key] = d

    # product resolution across drafts (after all products exist)
    product_owner: Dict[str, str] = {}
    akp_product: Dict[str, str] = {}
    for key, d in drafts.items():
        for pid, p in d["products"].items():
            product_owner[pid] = key
            akp_product.setdefault(p["akp"], pid)
    spine_products = set(spine.get("products", {}))

    def resolve_pd(tok: str) -> Optional[str]:
        tok = p_alias.get(tok, tok)
        if tok in spine_products:
            return tok
        return akp_product.get(tok)

    def _products_of(ref: str, me: str) -> List[str]:
        # a clause naming its sibling ("referred to in clause (a) above") only describes it - it is not a
        # condition, and waiting for it would make the two clauses wait for each other
        mine = _clause_parent(bundles[me].citation_key)
        return sorted(pid for pid, owner in product_owner.items()
                      if owner != me and _starts(bundles[owner].citation_key, ref)
                      and not (mine and _clause_parent(bundles[owner].citation_key) == mine))

    def _wire_reference(a: dict, src: dict, me: str) -> bool:
        """'specified in Schedule I' and Schedule I's paragraphs are steps here: the check becomes ANY of their
        results, so the engine uses those steps instead of asking again."""
        refs = [r for r in rlist(src, "reference_keys") + rlist(a, "reference_keys") if r]
        pds = sorted({p for r in dict.fromkeys(refs) for p in _products_of(r, me)})
        if not pds:
            return False
        d = drafts[me]
        nid = local_of[me][a["element"]]
        if len(pds) == 1:
            local_of[me][a["element"]] = pds[0]
        else:
            d["groups"][nid] = {"g": {"results": "", "element": nid}, "logic": "ANY", "members": pds, "wired": True}
        d["needs"] |= set(pds)
        report["warnings"].append(f"{me} {a['element']}: refers to {', '.join(dict.fromkeys(refs))} - "
                                  f"answered by the steps {', '.join(pds)}")
        return True

    local_of = {k: d["local"] for k, d in drafts.items()}
    for key, d in drafts.items():
        b, local = d["bundle"], d["local"]
        inline: Dict[str, str] = {}

        def ren(expr: str) -> str:
            def one(tok: str) -> str:
                if tok in R.KEYWORDS:
                    return tok
                if tok.startswith("PD_"):
                    pid = resolve_pd(tok)
                    if pid is None:
                        d["problems"].append(f"product {tok} is produced by no included MKP")
                        return tok
                    if product_owner.get(pid) != key:
                        d["needs"].add(pid)
                    return pid
                tok = inline.get(tok, tok)
                return local.get(tok, f_alias.get(tok, tok))
            parts = re.split(r"('(?:[^']|'')*')", expr or "")
            return "".join(p if p.startswith("'") else R.ID_TOKEN.sub(lambda m: one(m.group(0)), p) for p in parts)

        bool_facts = {f for f, s in list(spine_facts.items()) if s.get("type") == "BOOLEAN"}
        # predicates
        for a in b.of("A") + b.of("A_REF"):
            src = a
            if a["type"] == "A_REF":
                src = by_akp.get(a.get("same_akp_id")) or a
                if src is a or not src.get("results"):
                    d["problems"].append(f"{a['element']}: SAME atom {a.get('same_akp_id')} not found")
                    continue
            pid = local[a["element"]]
            wired = _wire_reference(a, src, key)
            if wired:
                continue
            facts = [f_alias.get(f, f) for f in rlist(src, "facts")]
            if a["type"] == "A_REF" and not facts:
                facts = [f_alias.get(f, f) for f in rlist(a, "facts")]
            for f in facts:
                use_fact(f)
            statement = rf(src, "statement") or src.get("concept") or a["element"]
            order = rf(src, "order").upper()
            if order not in ("LOW", "HIGH"):
                if order:
                    report["warnings"].append(f"{key} {a['element']}: order '{rf(src, 'order')}' is not LOW / HIGH - "
                                              f"{'LOW (it has a rule)' if rf(src, 'rule') else 'HIGH (no rule)'} used")
                order = "LOW" if rf(src, "rule") else "HIGH"
            pred = {"statement": statement[:500], "order": order}
            fi = rf(src, "false_if")
            if _sentence(fi):
                pred["if_false"] = fi[:300]
            if order == "LOW":
                raw_rule = rf(src, "rule")
                for tok in R.ID_TOKEN.findall(raw_rule):
                    if tok.startswith("F_"):
                        use_fact(tok)
                bool_facts |= {f for f, s in out_facts.items() if s["type"] == "BOOLEAN"}
                rule = value_aliases(R.clean(raw_rule, bool_facts, f_alias), cfg)
                declared = set(spine_facts) | set(out_facts)
                errs = R.check(rule, declared) if rule else ["no rule"]
                if not errs:
                    pred["rule"] = rule
                else:
                    pred["order"] = "HIGH"
                    report["warnings"].append(f"{key} {a['element']}: rule '{rf(src, 'rule')[:120]}' not usable "
                                              f"({'; '.join(errs)[:160]}) - decided by the judge instead")
            declared = set(spine_facts) | set(out_facts)
            if pred["order"] == "HIGH":
                grounding = [f for f in dict.fromkeys(facts) if f in declared]
                if not grounding:
                    d["problems"].append(f"{a['element']}: a HIGH check needs at least one known fact")
                    continue
                pred["interpretation"] = (rf(src, "interpretation") or f"Decide whether: {statement}")[:1000]
                pred["grounding"] = grounding
            d["predicates"][pid] = pred
        # groups (a one-member group is replaced by its member)
        for g in b.of("G"):
            members = rlist(g, "members")
            if len(members) < 2:
                if members:
                    inline[g["element"]] = members[0]
                    report["warnings"].append(f"{key} {g['element']}: one-member group replaced by {members[0]}")
                continue
            logic = rf(g, "logic").upper()
            if logic not in ("ALL", "ANY"):
                d["problems"].append(f"{g['element']}: logic '{logic}' is not ALL / ANY")
                continue
            d["groups"][local[g["element"]]] = {"g": g, "logic": logic, "members": members}
        for gid, gd in list(d["groups"].items()):
            if gd.get("wired"):
                d["groups"][gid] = {"name": f"Any of the referred steps ({gid})", "logic": "ANY",
                                    "members": list(gd["members"]),
                                    "description": "TRUE when any step of the law this check refers to is TRUE"}
                continue
            members = list(dict.fromkeys(ren(m) for m in gd["members"]))
            spec = {"name": (rf(gd["g"], "name") or gd["g"].get("concept") or gid)[:200], "logic": gd["logic"],
                    "members": members}
            if rf(gd["g"], "nl"):
                spec["description"] = rf(gd["g"], "nl")[:300]
            d["groups"][gid] = spec
        # outcomes + products
        for le in b.of("LE"):
            lid = local[le["element"]]
            o = {"outcome": (rf(le, "outcome") or le["element"])[:200], "expr": ren(rf(le, "expr")), "module": b.mkp_id or b.key}
            if rf(le, "nl"):
                o["description"] = rf(le, "nl")[:300]
            d["outcomes"][lid] = o
        for orow in b.of("OR"):
            ex = [local[e] for e in rlist(orow, "exclusive") if e in local]
            if len(ex) >= 2:
                d["exclusive"] = ex
        # triggers
        for ct in b.of("CT"):
            for tok in R.ID_TOKEN.findall(rf(ct, "trigger")):
                if tok.startswith("F_"):
                    use_fact(tok)
            bool_facts |= {f for f, s in out_facts.items() if s["type"] == "BOOLEAN"}
            trig = R.clean(rf(ct, "trigger"), bool_facts, f_alias)
            if not trig or R.check(trig, set(spine_facts) | set(out_facts)):
                report["warnings"].append(f"{key} {ct['element']}: review flag left out (trigger '{rf(ct, 'trigger')[:80]}' not usable)")
                continue
            beside = next(iter(d["outcomes"]))
            for o in rlist(ct, "outcomes"):
                if local.get(o) in d["outcomes"]:
                    beside = local[o]
                    break
            q = rf(ct, "question")
            if q:
                d["triggers"][local[ct["element"]]] = {"question": q[:500], "trigger": trig, "beside": beside}

    # ---- drop drafts with problems, and everything that needs their products (until stable)
    changed = True
    while changed:
        changed = False
        alive = {pid for k, d in drafts.items() for pid in d["products"]} | spine_products
        for key in list(drafts):
            d = drafts[key]
            missing = [p for p in d["needs"] if p not in alive]
            if d["problems"] or missing:
                report["excluded"].append({"mkp": key, "reason": "; ".join(d["problems"] + [f"needs {p}, which is left out" for p in missing])[:600]})
                drafts.pop(key)
                changed = True

    # ---- node checks: every id used by an expression must exist
    nodes = set()
    for section in ("predicates", "groups", "products"):
        nodes |= set(spine.get(section, {}))
    for d in drafts.values():
        nodes |= set(d["predicates"]) | set(d["groups"]) | set(d["products"])
    for key in list(drafts):
        d = drafts[key]
        bad = []
        for gid, g in d["groups"].items():
            bad += [f"{gid} member {m} is not defined" for m in g["members"] if m not in nodes]
        for lid, o in d["outcomes"].items():
            bad += [f"{lid}: {e}" for e in R.check(o["expr"], set(), nodes)]
        if bad:
            report["excluded"].append({"mkp": key, "reason": "; ".join(bad)[:600]})
            drafts.pop(key)

    # ---- overrides (X overrides Y, both included)
    overrides: Dict[str, dict] = OrderedDict()
    owner_of: Dict[str, List[str]] = {}
    for key, d in drafts.items():
        b = d["bundle"]
        for other, od in drafts.items():
            if other == key:
                continue
            ob = od["bundle"]
            rel = [(rf(lk, "relation").upper(), rf(lk, "target")) for lk in b.of("LK")]
            hit = any(r == "OVERRIDDEN_BY" and _starts(ob.citation_key, t) for r, t in rel) or \
                any(rf(lk, "relation").upper() == "OVERRIDES" and _starts(b.citation_key, rf(lk, "target")) for lk in ob.of("LK"))
            if hit:
                src = next(iter(od["outcomes"].items()))
                oid = new_id(f"IH_{ob.tag}_{b.tag}", ob, "OVERRIDE")
                overrides[oid] = {"outcome": src[1]["outcome"], "expr": src[1]["expr"], "module": src[1]["module"],
                                  **({"description": src[1]["description"]} if "description" in src[1] else {})}
                owner_of.setdefault(key, []).append(oid)
                for p in od["products"]:
                    d["needs"].add(p)

    # ---- order: definitions first, then dependencies (products), else AKP order
    keys = list(drafts)
    first_of_parent: Dict[str, int] = {}
    for i, k in enumerate(keys):
        par = _clause_parent(drafts[k]["bundle"].citation_key) or k
        first_of_parent.setdefault(par, i)
    # definitions first; clauses of one provision kept next to each other (so they can share one step)
    prio = {k: (0 if drafts[k]["bundle"].knowledge_type == "DEFINITION" else 1,
                first_of_parent[_clause_parent(drafts[k]["bundle"].citation_key) or k], i) for i, k in enumerate(keys)}
    deps = {k: {product_owner[p] for p in drafts[k]["needs"] if product_owner.get(p) in drafts and product_owner[p] != k}
            for k in keys}
    ordered, done = [], set()
    while len(ordered) < len(keys):
        ready = sorted([k for k in keys if k not in done and deps[k] <= done], key=lambda k: prio[k])
        if not ready:
            loop = sorted([k for k in keys if k not in done], key=lambda k: prio[k])
            report["blocking"].append("product cycle between: " + ", ".join(loop))
            ready = loop[:1]
        ordered.append(ready[0])
        done.add(ready[0])

    # ---- assemble
    rb = {"name": f"{tsg} {tsg_name} - GOVERNANCE MASTER rulebook", "version": version,
          "facts": dict(spine.get("facts", {})), "predicates": dict(spine.get("predicates", {})),
          "groups": dict(spine.get("groups", {})), "outcomes": dict(spine.get("outcomes", {})),
          "overrides": dict(spine.get("overrides", {})), "triggers": dict(spine.get("triggers", {})),
          "products": dict(spine.get("products", {})), "compounds": list(spine.get("compounds", [])),
          "precedents": [dict(p, rulebook_version=version) for p in spine.get("precedents", [])]}
    used_facts: Set[str] = set()
    merged_parent: dict = {}
    step_of: Dict[str, dict] = {}
    done_before: Set[str] = set()
    for key in ordered:
        d = drafts[key]
        b = d["bundle"]
        rb["predicates"].update(d["predicates"])
        rb["groups"].update(d["groups"])
        rb["outcomes"].update(d["outcomes"])
        rb["triggers"].update(d["triggers"])
        for pid, p in d["products"].items():
            rb["products"][pid] = {"statement": p["statement"]}
        parent = _clause_parent(b.citation_key) if cfg.get("_merge_clauses", True) else ""
        target = merged_parent.get(parent) if parent else None
        if target and not deps[key] <= target["before"]:
            between = done_before - target["before"] - set(target["keys"])
            if not any(deps[k] & set(target["keys"]) for k in between):
                rb["compounds"].remove(target["comp"])     # nothing in between needs it: the shared step moves here
                rb["compounds"].append(target["comp"])
                target["before"] = set(done_before) - set(target["keys"])
        if target and deps[key] <= target["before"]:
            comp = target["comp"]                # another clause of the same provision: same step
            if len(target["keys"]) == 1:          # first merge: the step is named after the provision
                comp["id"] = new_id("C_" + (parent.split("_", 1)[1] if "_" in parent else parent), b, "COMPOUND")
            comp["includes"] += list(d["outcomes"])
            comp["produces"] += [{"product": pid, "from": [p["le"]]} for pid, p in d["products"].items()]
            if d["exclusive"]:
                comp.setdefault("exclusive", []).extend(e for e in d["exclusive"] if e in d["outcomes"])
            q = snap.questions.get(b.key)
            comp["question"] = (q or f"{_strip_clause(b.citation or b.citation_key)}: which clause applies?")[:300]
            target["keys"].append(key)
            for k_ in target["keys"]:
                step_of[k_] = comp
        else:
            cid = new_id(f"C_{b.tag}", b, "COMPOUND")
            concept = b.concept if b.concept and b.concept.upper() != "NA" else "does it apply"
            question = snap.questions.get(b.key) or f"{b.citation or b.citation_key}: {concept}?"
            comp = {"id": cid, "question": question[:300], "includes": list(d["outcomes"]),
                    "produces": [{"product": pid, "from": [p["le"]]} for pid, p in d["products"].items()]}
            if d["exclusive"]:
                comp["exclusive"] = [e for e in d["exclusive"] if e in d["outcomes"]]
            rb["compounds"].append(comp)
            step_of[key] = comp
            if parent:
                merged_parent[parent] = {"comp": comp, "keys": [key], "before": set(done_before)}
        done_before.add(key)
        mine = owner_of.get(key, [])
        if mine:
            comp.setdefault("overrides", []).extend(m for m in mine if m not in comp.get("overrides", []))
            for oid in mine:
                rb["overrides"][oid] = overrides[oid]
        for p in d["predicates"].values():
            used_facts |= set(R.parse(p["rule"])["facts"]) if "rule" in p else set(p.get("grounding", []))
        for t in d["triggers"].values():
            used_facts |= R.parse(t["trigger"])["facts"]
    for fid, spec in out_facts.items():
        if fid in used_facts and fid not in rb["facts"]:
            rb["facts"][fid] = spec
    for section in ("overrides", "triggers"):
        if not rb[section]:
            rb.pop(section)
    if not rb["precedents"]:
        rb.pop("precedents")

    # ---- duplicate-fact guard and coverage
    for fid in rb["facts"]:
        dup = (snap.facts.get(fid) or {}).get("possible_duplicate_of")
        if dup and dup in rb["facts"]:
            report["blocking"].append(f"facts {fid} and {dup} may mean the same thing - merge them in the fact registry")
    included = set(ordered)
    # ---- which of your atoms each spine check stands for (same rule after the fact aliases)
    spine_rules = {pid: re.sub(r"\s+", " ", str(p.get("rule", ""))).strip()
                   for pid, p in spine.get("predicates", {}).items() if p.get("rule")}
    by_rule: Dict[str, List[str]] = {}
    for pid, r in spine_rules.items():
        by_rule.setdefault(r, []).append(pid)
    report["spine_partners"] = []
    for key in report["covered_by_spine"]:
        b = bundles.get(key)
        if not b:
            continue
        sbool = {f for f, s_ in spine_facts.items() if s_.get("type") == "BOOLEAN"}
        for a in b.of("A"):
            raw = rf(a, "rule")
            if not raw:
                continue
            r = value_aliases(R.clean(raw, sbool, f_alias), cfg)
            for pid in by_rule.get(r, []):
                report["spine_partners"].append({"spine_id": pid, "source_mkp": key, "akp_id": a.get("akp_id") or "",
                                                 "atom": a["element"], "statement": rf(a, "statement")[:200]})
    report["coverage"] = []
    for m in report["member_atoms"]:
        state = ("IN_SPINE" if m["source_mkp"] in report["covered_by_spine"] else
                 "INCLUDED" if m["source_mkp"] in included else "LEFT_OUT")
        report["coverage"].append({**m, "state": state})
    report["included"] = [{"mkp": k, "reason": chosen.get(k, "")} for k in ordered]
    report["step_of_mkp"] = {k: c["id"] for k, c in step_of.items()}
    # the manager's "what is inside one molecule" table, for every law in this rulebook
    rid = {(m["source_mkp"], m["akp_element"]): m["rulebook_id"] for m in id_map}
    report["molecules"] = []
    for k in ordered:
        b = bundles[k]
        for r in b.rows:
            if r["type"] in ("LE", "OR", "G", "PD", "A", "A_REF", "CT"):
                report["molecules"].append({"molecule": k, "mkp_id": b.mkp_id, "element_type": r["type"],
                                            "element_id": r["element"], "rulebook_id": rid.get((k, r["element"]), ""),
                                            "concept": r.get("concept") or ""})
        for f in dict.fromkeys(f_alias.get(f, f) for a in b.of("A") for f in rlist(a, "facts")):
            report["molecules"].append({"molecule": k, "mkp_id": b.mkp_id, "element_type": "F", "element_id": f,
                                        "rulebook_id": f if f in rb["facts"] else "",
                                        "concept": (snap.facts.get(f) or {}).get("name", "")})
        report["molecules"].append({"molecule": k, "mkp_id": b.mkp_id, "element_type": "TSG", "element_id": tsg,
                                    "rulebook_id": version, "concept": tsg_name})
    report["counts"] = {s: len(rb.get(s, {})) for s in ("facts", "predicates", "groups", "outcomes", "overrides",
                                                         "triggers", "products", "compounds")}
    built = Built(rb, report, id_map, [], ordered)
    built.tests = tests_from_tc(drafts, ordered, rb, cfg, f_alias)
    return built


# ---------------------------------------------------------------- 4. AKP test cases -> engine transactions

PAIR = re.compile(r"([A-Z][A-Z0-9_]*)\s*[:=]\s*('(?:[^']|'')*'|\"[^\"]*\"|[^,;\]}\s]+)")


def _value(raw: str, spec: Optional[dict]):
    v = raw.strip().strip("'\"")
    up = v.upper()
    if spec and spec.get("type") == "BOOLEAN":
        return {"TRUE": True, "YES": True, "FALSE": False, "NO": False}.get(up, None)
    if spec and spec.get("type") == "AMOUNT":
        try:
            float(v)
            return float(v) if "." in v else int(v)
        except ValueError:
            return None
    if up == "NULL":
        return None
    return v


def _test_pack(d: dict, rb: dict, name: str) -> Tuple[dict, Dict[str, str]]:
    """A one-step rulebook that checks exactly one MKP's converted logic, as AKP tested it.
    Products of other steps (and of the spine) become test facts F_T_<name> read by checks A_T_<name>, so a
    test case can set them (default TRUE: the prerequisite holds). Overrides from other MKPs are left out."""
    products_in = sorted({t for o in d["outcomes"].values() for t in R.ID_TOKEN.findall(o["expr"])
                          if t.startswith("PD_") and t not in d["products"]} |
                         {m for g in d["groups"].values() for m in g["members"] if m.startswith("PD_") and m not in d["products"]})
    swap = {pd: "A_T_" + pd[3:] for pd in products_in}
    facts, preds = {}, {}
    for pd in products_in:
        facts["F_T_" + pd[3:]] = {"name": f"Test input: {rb['products'].get(pd, {}).get('statement', pd)}"[:200],
                                  "type": "BOOLEAN"}
        preds[swap[pd]] = {"statement": f"Test input {pd} holds", "order": "LOW", "rule": f"F_T_{pd[3:]} = TRUE"}
    used = set(facts)
    for pid, p in d["predicates"].items():
        preds[pid] = p
        used |= set(R.parse(p["rule"])["facts"]) if "rule" in p else set(p.get("grounding", []))
    for f in used - set(facts):
        facts[f] = rb["facts"][f]
    groups = {gid: dict(g, members=[swap.get(m, m) for m in g["members"]]) for gid, g in d["groups"].items()}
    outcomes = {lid: dict(o, expr=R.rename(o["expr"], swap)) for lid, o in d["outcomes"].items()}
    comp = {"id": "C_TEST", "question": name, "includes": list(outcomes),
            "produces": [{"product": pid, "from": [p["le"]]} for pid, p in d["products"].items()]}
    if d["exclusive"]:
        comp["exclusive"] = d["exclusive"]
    pack = {"name": f"GOVERNANCE MASTER test pack {name}", "version": "test", "facts": facts, "predicates": preds,
            "groups": groups, "outcomes": outcomes,
            "products": {pid: {"statement": p["statement"]} for pid, p in d["products"].items()}, "compounds": [comp]}
    return pack, {pd: "F_T_" + pd[3:] for pd in products_in}


def tests_from_tc(drafts, ordered, rb, cfg, f_alias) -> List[dict]:
    out = []
    for key in ordered:
        d = drafts[key]
        b = d["bundle"]
        rev = dict(d["local"])
        first_le = next(iter(d["outcomes"]))
        tcs = b.of("TC")
        if not tcs:
            continue
        name = re.sub(r"[^A-Z0-9_]", "_", f"TC_{b.tag}".upper())
        pack, product_facts = _test_pack(d, rb, f"{b.citation or b.citation_key} (AKP test cases)")
        p_alias = {k: v for k, v in cfg.get("product_aliases", {}).items() if not k.startswith("_")}
        for tc in tcs:
            facts = {f: True for f in product_facts.values()}
            judg = {}
            for f, raw in PAIR.findall(rf(tc, "facts")) + PAIR.findall(rf(tc, "products")):
                if f.startswith("PD_"):
                    target = product_facts.get(p_alias.get(f, f)) or product_facts.get(rev.get(f, f))
                    val = _value(raw, {"type": "BOOLEAN"})
                    if target and val is not None:
                        facts[target] = val
                    continue
                f = f_alias.get(f, f)
                if f in pack["facts"]:
                    val = _value(raw, pack["facts"][f])
                    if val is not None:
                        facts[f] = val
            for a, raw in PAIR.findall(rf(tc, "given_atoms")):
                pid = rev.get(a, a)
                val = _value(raw, {"type": "BOOLEAN"})
                if pid in pack["predicates"] and pack["predicates"][pid]["order"] == "HIGH" and val is not None:
                    judg[pid] = val
            expect: Dict[str, Optional[bool]] = {}
            text = rf(tc, "expect") or rf(tc, "outcome")
            pairs = PAIR.findall(text)
            if pairs:
                for le, raw in pairs:
                    lid = rev.get(le, le)
                    if lid in d["outcomes"]:
                        expect[lid] = _value(raw, {"type": "BOOLEAN"})
            elif text.strip().upper() in ("TRUE", "FALSE", "UNKNOWN"):
                expect[first_le] = _value(text, {"type": "BOOLEAN"})
            products = {p["le"]: pid for pid, p in d["products"].items()}
            want = {products[le]: ("UNKNOWN" if v is None else ("TRUE" if v else "FALSE")) for le, v in expect.items()
                    if le in products}
            if not want:
                continue
            tid = re.sub(r"[^A-Z0-9_]", "_", f"{name}_{tc['element']}".upper())
            out.append({"id": tid, "source_mkp": key, "title": rf(tc, "name") or tc["element"], "pack": name,
                        "rulebook": pack,
                        "transaction": {"id": tid, "description": rf(tc, "name") or "", "facts": facts,
                                        "judgments": judg},
                        "expect": want})
    return out
