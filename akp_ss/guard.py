"""AKP / Python checks on the Standard AKP table, before the comparison model.

legal_words(source_text, rows)    negation, qualifiers, modals and numbers of the MKP must survive in the atoms
                                  ("without consideration" -> an atom saying "for consideration" is an ERROR)
structure(rows, source, ...)      IDs, LE / G logic, reachability, SP coverage, facts declared, fact per atom
Each returns a list of short, fixable error sentences (empty = pass). They go back to the judge as-is.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set

from transport import get_field

from .parse import field_list, rfield

NEGATIONS = ("without", "not", "neither", "nor", "no", "never", "other than", "except", "excluding", "non")
NEG_MARKERS = {"without", "not", "no", "neither", "nor", "never", "none", "nil", "non", "except", "excluding",
               "other", "absent", "false", "unless", "cannot", "nothing", "zero", "free"}
QUALIFIERS = ("notwithstanding", "subject to", "provided that", "unless", "except", "only", "deemed", "either",
              "whichever is", "not later than", "within", "on or before", "at least", "not exceeding", "exceeding")
ATOM_FIELDS = ("statement", "rule", "false_if", "interpretation", "nl")
NUM = re.compile(r"(?<![A-Za-z_\d])(\d[\d,]*(?:\.\d+)?)(?![A-Za-z_\d])")


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "").lower().replace("―", '"').replace("‖", '"')).strip()


def _tokens(t: str) -> List[str]:
    t = _norm(t).replace("!=", " not ").replace("<>", " not ")
    return re.findall(r"[a-z0-9]+", t)


def _logic_text(rows: List[dict]) -> List[str]:
    """Texts that carry legal meaning (not the copied SP text, not the MKP row, not TC examples)."""
    out = []
    for r in rows:
        t = r["TYPE"]
        if t in ("A", "PD", "CT"):
            out.append(" ".join(get_field(r, f) for f in ATOM_FIELDS + ("question",)))
        elif t in ("LE", "G", "OR"):
            out.append(r["RESULTS"])
    return out


def _numbers(text: str) -> Set[str]:
    return {m.group(1).replace(",", "").rstrip(".") for m in NUM.finditer(text or "")}


def legal_words(source_text: str, rows: List[dict]) -> List[str]:
    src = _norm(source_text)
    if not src or not any(r["TYPE"] == "A" for r in rows):
        return []
    errors = []
    texts = _logic_text(rows)
    joined = _norm(" ".join(texts))
    lw = _norm(" ".join(r["RESULTS"] for r in rows if r["TYPE"] == "LW"))
    atom_rows = [r for r in rows if r["TYPE"] == "A"]

    # 1. negation must stay with the word it negates: "without consideration" -> some element keeps
    #    a negation marker within 4 words of "consideration" (statement, rule, false_if, LE NOT ...)
    tok_texts = [_tokens(t) for t in texts]
    for m in re.finditer(r"\b(without|not|neither|nor|no|never|other than|except|excluding)\s+(?:a |an |the |any |be |been |by |to |of |as )*([a-z][a-z-]{2,})", src):
        word, target = m.group(1), m.group(2).split("-")[0]
        if word == "no" and re.match(r"no\.", src[m.start():m.start() + 3]):
            continue
        stem = target[:6]
        carried, flipped = False, []
        for toks in tok_texts:
            for i, tk in enumerate(toks):
                if tk.startswith(stem):
                    window = toks[max(0, i - 4): i + 5]
                    if any(w in NEG_MARKERS for w in window):
                        carried = True
                        break
            if carried:
                break
        if not carried:
            for r in atom_rows:
                st = _norm(get_field(r, "statement"))
                if re.search(rf"\b(with|for|on|by|has|have|having|is|are)\s+(?:a |an |the |any )*{re.escape(stem)}", st):
                    flipped.append(r["ELEMENT"])
            ctx = src[max(0, m.start() - 30): m.end() + 30].strip()
            if flipped:
                errors.append(f"NEGATION FLIPPED: source says '{m.group(0)}' (...{ctx}...) but {', '.join(flipped)} "
                              f"state it positively and no element keeps the negation")
            elif any(stem in t for t in (joined,)):
                errors.append(f"NEGATION LOST: source says '{m.group(0)}' but no atom, rule, false_if or LE keeps the "
                              f"negation next to '{target}'")
            else:
                errors.append(f"MISSING: source says '{m.group(0)}' (...{ctx}...) - no element represents it")

    # 2. qualifiers that set priority or scope must appear in an element (or its LW legal-word row)
    for q in QUALIFIERS:
        if re.search(rf"\b{re.escape(q)}\b", src) and not re.search(rf"\b{re.escape(q)}\b", joined + " " + lw):
            errors.append(f"QUALIFIER LOST: source has '{q}' but no atom, LE, OR or LW row carries it")

    # 3. modal verbs must not change
    atoms_text = _norm(" ".join(get_field(r, "statement") + " " + get_field(r, "rule") for r in atom_rows))
    src_shall = bool(re.search(r"\bshall\b(?! not\b)|\bmust\b|\bis required to\b", src))
    src_may = bool(re.search(r"\bmay\b(?! not\b)", src))
    if src_shall and not src_may and re.search(r"\bmay\b(?! not\b)", atoms_text):
        errors.append("MODAL CHANGED: source says 'shall' (mandatory) but an atom says 'may'")
    if src_may and not src_shall and re.search(r"\b(shall|must)\b(?! not\b)", atoms_text):
        errors.append("MODAL CHANGED: source says 'may' (power / option) but an atom says 'shall' or 'must'")
    for neg_modal in ("shall not", "may not"):
        if re.search(rf"\b{neg_modal}\b", src) and not re.search(r"\b(not|no|prohibit\w*|cannot|never)\b", atoms_text):
            errors.append(f"MODAL CHANGED: source says '{neg_modal}' but no atom keeps the prohibition")

    # 4. numbers: every amount / rate / period of the source kept, none invented in atoms
    src_nums = _numbers(source_text)
    all_text = " ".join(r["RESULTS"] for r in rows if r["TYPE"] not in ("SP", "MKP", "TC", "SUM")) + " " + \
        " ".join(r["CONCEPT"] for r in rows)
    kept = _numbers(all_text)
    missing = sorted(n for n in src_nums - kept if len(n) > 1 or "." in n)
    if missing:
        errors.append(f"NUMBER LOST: source numbers {missing} are not in any atom, fact, rule or LW row")
    invented = sorted(_numbers(" ".join(get_field(r, "statement") + " " + get_field(r, "rule") for r in atom_rows)) - src_nums)
    invented = [n for n in invented if n not in ("0", "1")]
    if invented:
        errors.append(f"NUMBER ADDED: atoms contain {invented}, which the source does not state")
    return errors


# ---------------------------------------------------------------- structure

ID_TOKEN = re.compile(r"\b(?:A|G|PD|F|LE|CT)_[A-Za-z0-9_]+\b")


def structure(rows: List[dict], sp_source: List[dict], operative: bool, registry_ids: Iterable[str] = ()) -> List[str]:
    errors = []
    registry_ids = set(registry_ids)
    by_type: Dict[str, Dict[str, dict]] = {}
    for r in rows:
        bucket = by_type.setdefault(r["TYPE"], {})
        if r["ELEMENT"] in bucket and r["TYPE"] not in ("SUM", "LW"):
            errors.append(f"duplicate {r['TYPE']} id {r['ELEMENT']}")
        bucket[r["ELEMENT"]] = r
    atoms, groups, pds = by_type.get("A", {}), by_type.get("G", {}), by_type.get("PD", {})
    facts = by_type.get("FACTS", {})
    les = by_type.get("LE", {})
    known = set(atoms) | set(groups) | set(pds) | set(by_type.get("A_REF", {}))
    if operative and (not atoms and not by_type.get("A_REF")) :
        errors.append("operative MKP has no A rows")
    if operative and not les:
        errors.append("operative MKP has no LE row")

    # LE / G logic
    for le_id, le in les.items():
        expr = get_field(le, "expr")
        if re.search(r"\bF_", expr):
            errors.append(f"{le_id}: LE expr must not contain F_ facts ({expr})")
        for tok in ID_TOKEN.findall(expr):
            if tok.startswith(("A_", "G_", "PD_")) and tok not in known:
                errors.append(f"{le_id}: expr uses {tok}, which is not defined")
        if not re.search(r"\b(?:A|G|PD)_", expr):
            errors.append(f"{le_id}: expr has no A_ / G_ / PD_ terms")
    for g_id, g in groups.items():
        logic = get_field(g, "logic").upper()
        members = field_list(g, "members")
        if logic not in ("ALL", "ANY"):
            errors.append(f"{g_id}: logic must be ALL or ANY (got '{logic}')")
        if len(members) < 2:
            errors.append(f"{g_id}: a group needs two or more members")
        for m in members:
            if m not in known:
                errors.append(f"{g_id}: member {m} is not defined")

    # every atom reachable from an LE
    reach, stack = set(), [t for le in les.values() for t in ID_TOKEN.findall(get_field(le, "expr"))]
    while stack:
        t = stack.pop()
        if t in reach:
            continue
        reach.add(t)
        if t in groups:
            stack.extend(field_list(groups[t], "members"))
    for a in atoms:
        if les and a not in reach:
            errors.append(f"{a}: atom is not used by any LE or group (orphan)")

    # atoms: complete proposition + facts
    for a_id, a in atoms.items():
        st = get_field(a, "statement")
        if len(st.split()) < 4:
            errors.append(f"{a_id}: statement '{st}' is a fragment - write the full legal proposition")
        used = field_list(a, "facts")
        if operative and not used and not get_field(a, "pd") and not field_list(a, "products"):
            errors.append(f"{a_id}: every operative atom must name the fact(s) it tests (facts: [F_...]) or the PD it uses")
        for f in used:
            if f not in facts and f not in registry_ids:
                errors.append(f"{a_id}: fact {f} is not declared in a FACTS row or the fact registry")
    for f_id, f in facts.items():
        if f["MKP"] != "TXN":
            errors.append(f"{f_id}: FACTS rows are owned by TXN")
        if not re.fullmatch(r"F_[A-Z0-9]+(?:_[A-Z0-9]+)+", f_id):
            errors.append(f"{f_id}: fact id must be F_<ENTITY>_<ATTRIBUTE> in capitals")
        if not rfield(f, "type"):
            errors.append(f"{f_id}: fact needs a type")

    # SP coverage: every source phrase copied and mapped
    std_sp = by_type.get("SP", {})
    for sp in sp_source:
        sid, text = get_field(sp, "sp"), get_field(sp, "text")
        if not sid:
            continue
        row = std_sp.get(sid)
        if row is None:
            errors.append(f"{sid}: source phrase not accounted for in an SP row")
            continue
        if "".join(_tokens(get_field(row, "text"))) != "".join(_tokens(text)):
            errors.append(f"{sid}: SP text must be copied exactly from the approved MKP phrase")
        for el in field_list(row, "elements"):
            if el.startswith(("A_", "G_", "LE_", "PD_", "CT_")) and el not in known | set(les) | set(by_type.get("CT", {})):
                errors.append(f"{sid}: elements lists {el}, which is not defined")
    return errors


# ---------------------------------------------------------------- engine-ready atoms (less hallucination)

def engine_ready(rows: List[dict], registry: dict = None) -> List[str]:
    """Every atom must be usable by the Decision Graph Engine as written:
      order is LOW or HIGH;  LOW -> a rule in the engine grammar on declared facts, values only from the fact's
      options;  HIGH -> an interpretation question and at least one fact.  Errors go back to the judge."""
    from governance_master_ss import rules as R
    registry = registry or {}
    facts, bool_facts = {}, set()
    for r in rows:
        if r["TYPE"] == "FACTS":
            opts = [str(o).strip().strip("'\"") for o in _field_list(r, "options") if str(o).strip().upper() != "NA"]
            ftype = get_field(r, "type").upper()
            facts[r["ELEMENT"]] = {"type": ftype, "options": opts}
            if ftype in ("BOOL", "BOOLEAN"):
                bool_facts.add(r["ELEMENT"])
    known = set(facts) | set(registry)
    errs = []
    for r in rows:
        if r["TYPE"] != "A":
            continue
        el = r["ELEMENT"]
        order = get_field(r, "order").upper()
        atom_facts = _field_list(r, "facts")
        if order not in ("LOW", "HIGH"):
            errs.append(f"ORDER: {el} order must be LOW or HIGH, not '{get_field(r, 'order')}' "
                        "(LOW = a rule on facts decides it; HIGH = the judge decides it)")
            continue
        if order == "LOW":
            rule = get_field(r, "rule")
            if not rule or rule.upper() == "NA":
                errs.append(f"RULE: {el} is LOW but has no rule; write a rule on its facts or make it HIGH")
                continue
            clean = R.clean(rule, bool_facts, {})
            problems = R.check(clean, known)
            if problems:
                errs.append(f"RULE: {el} rule '{rule[:120]}' is not engine grammar ({'; '.join(problems)[:160]}). "
                            "Use = != > >= < <=, IN ['A','B'], IS_SET, AND / OR / NOT, quoted 'VALUES' only")
                continue
            for fid, vals in R.values_used(clean).items():
                opts = facts.get(fid, {}).get("options") or []
                bad = sorted(v for v in vals if opts and v not in opts)
                if bad:
                    errs.append(f"VALUE: {el} compares {fid} with {bad}, which are not among its options {opts}")
        else:
            if not get_field(r, "interpretation") or get_field(r, "interpretation").upper() == "NA":
                errs.append(f"QUESTION: {el} is HIGH but has no interpretation (the question the judge answers)")
            if not atom_facts:
                errs.append(f"FACTS: {el} is HIGH but names no fact the judge can look at")
    return errs


def _field_list(row: dict, name: str) -> List[str]:
    raw = get_field(row, name)
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [x.strip() for x in raw.split(",") if x.strip()]
