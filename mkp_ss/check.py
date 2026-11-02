"""MKP / Python checks around the LLM calls.

pregate(rows, ctx)            -> report text for the judge (PRE-GATE REPORT input of the judge prompt)
judge_rows(raw)               -> (final 4-column rows, 7-column audit rows)          [old _judge_rows]
standard_gate(rows, ctx, ...) -> (rows, warnings)  or raises GateError(list of errors)
                                 = old _validate_standard rules + FIDELITY: every operative FINAL text must
                                   have the same words, in the same order, as the Python reference text
                                   built from the IKP source (punctuation, case and spacing ignored).
Python only fixes structural metadata (parent, heading part); it never edits legal text.
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from transport import clean, derive_id, get_field, parse_model_output

ROW_TYPES = {"BATCH", "MKP", "SP", "SUM", "UNC", "UNL"}
CHILD_LEVELS = {"SUBSECTION", "SUBRULE", "CLAUSE", "SUBCLAUSE", "PROVISO", "EXPLANATION", "ILLUSTRATION",
                "ITEM", "SUBITEM", "SUBPARAGRAPH"}

# Default word patterns used when word_lists config is absent (matches permission.json defaults).
_DEFAULT_NUMBER_PATTERN = r"\b\d[\d,./]*(?:\s*(?:percent|%|years?|months?|days?|hours?))?"
_DEFAULT_DATE_PATTERN = (
    r"\b(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
    r"|(?:\d{1,2}(?:st|nd|rd|th)?\s+)?(?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December)(?:\s+\d{4})?)\b"
)


class GateError(RuntimeError):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("MKP GATE: " + " || ".join(errors[:8]) + (f" (+{len(errors) - 8} more)" if len(errors) > 8 else ""))


def four(rows) -> List[dict]:
    return [{k: clean(r.get(k, "")) for k in ("CLUSTER", "TYPE", "ELEMENT", "RESULTS")} for r in rows]


def parse_candidate(raw: str) -> List[dict]:
    return four(parse_model_output(raw or ""))


def judge_rows(raw: str):
    text = re.sub(r"^```[^\n]*\n|\n?```\s*$", "", clean(raw))
    lines = text.splitlines()
    header = "CLUSTER|TYPE|ELEMENT|MODEL_1|MODEL_2|FINAL|CONFIDENCE"
    start = next((i for i, line in enumerate(lines) if line.strip().replace(" ", "") == header), None)
    if start is None:
        raise GateError(["judge did not return the seven-column table CLUSTER|TYPE|ELEMENT|MODEL_1|MODEL_2|FINAL|CONFIDENCE"])
    audit, final, bad = [], [], []
    for line in lines[start + 1:]:
        if not line.strip() or line.startswith("```"):
            continue
        fields = [v.strip() for v in line.split("|", 6)]
        if len(fields) != 7:
            bad.append(f"judge row is not seven columns: {line[:100]}")
            continue
        item = dict(zip(header.split("|"), fields))
        audit.append(item)
        if item["TYPE"] == "REJ" or item["FINAL"] in {"NONE", "REJECTED", "NA"}:
            continue
        if item["TYPE"] not in ROW_TYPES:
            bad.append(f"unexpected judge TYPE {item['TYPE']}")
            continue
        final.append({"CLUSTER": item["CLUSTER"], "TYPE": item["TYPE"], "ELEMENT": item["ELEMENT"], "RESULTS": item["FINAL"]})
    if bad:
        raise GateError(bad)
    return final, audit


# ---------------------------------------------------------------- fidelity

def _tokens(text: str) -> List[str]:
    t = str(text or "").lower().replace("₹", " rupees ").replace("%", " per cent ").replace("―", '"').replace("‖", '"')
    return re.findall(r"[a-z0-9]+", t)


def fidelity(text: str, reference: str) -> str:
    """'' when the words match the reference, else a short description of the difference."""
    a, b = _tokens(reference), _tokens(text)
    if "".join(a) == "".join(b):          # same letters - only spacing / hyphenation / punctuation differ
        return ""
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        src, got = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        out.append({"delete": f"drops '{src}'", "insert": f"adds '{got}'",
                    "replace": f"'{src}' -> '{got}'"}[op])
    return "; ".join(out[:4]) + (" ..." if len(out) > 4 else "")


def _reference(ctx: dict, citation: str, part: str, kt: str):
    refs = ctx["references"]
    if kt == "HEADING" or part == "HEAD":
        return refs.get((citation, "HEADING"))
    return refs.get((citation, part))


# ---------------------------------------------------------------- pre-gate (input for the judge)

def _check_rows(rows: List[dict], ctx: dict) -> Tuple[Dict[str, List[str]], List[str]]:
    """Per-element problems and table-level problems (used for the pre-gate report)."""
    per, table = {}, []
    expected = set(ctx["expected"])
    shared = {s["owner"] for s in ctx["cluster"]["shared_amendments"]}
    sps: Dict[str, List[dict]] = {}
    for r in rows:
        if r["TYPE"] == "SP":
            sps.setdefault(r["ELEMENT"], []).append(r)
    acronyms: Dict[str, str] = {}
    mkps = [r for r in rows if r["TYPE"] == "MKP"]
    for r in mkps:
        el, errs = r["ELEMENT"], []
        if "#" not in el:
            per[el] = ["ELEMENT has no #part"]
            continue
        citation, part = el.split("#", 1)
        kt = get_field(r, "knowledge_type").upper()
        if citation not in expected and not (citation in shared and part.startswith("AMD")) and part != "HEAD":
            errs.append("not in the EXPECTED PROVISION LIST")
        text = get_field(r, "text")
        if not text:
            errs.append("empty text")
        phrases = [get_field(s, "text") for s in sps.get(el, [])]
        if not phrases or " ".join(phrases) != text:
            errs.append("SP rows do not rejoin to the text")
        concept = get_field(r, "concept")
        words = concept.split()
        if not 3 <= len(words) <= 7:
            errs.append(f"concept has {len(words)} words")
        if concept:
            acr = derive_id("M", concept)[2:]
            if len(acr) > 10:
                errs.append(f"acronym {acr} longer than 10")
            if acr in acronyms and acronyms[acr] != el:
                errs.append(f"acronym {acr} also used by {acronyms[acr]}")
            acronyms.setdefault(acr, el)
        ref = _reference(ctx, citation, part, kt)
        if ref is not None and kt != "HEADING" and part == "TEXT" and text:
            diff = fidelity(text, ref.text)
            if diff:
                errs.append(f"FIDELITY {diff}")
        per[el] = errs
    covered = {r["ELEMENT"].split("#", 1)[0] for r in mkps}
    missing = [k for k in ctx["expected"] if k not in covered]
    if missing:
        table.append(f"no MKP for {', '.join(missing)}")
    got = {r["ELEMENT"] for r in mkps}
    for (ck, part), d in ctx["references"].items():
        if part.startswith("AMD") and f"{ck}#{part}" not in got:
            table.append(f"no amendment MKP {ck}#{part} (footnote marker in the source)")
    return per, table


def pregate(gemini_rows: List[dict], claude_rows: List[dict], ctx: dict) -> str:
    lines = ["PYTHON REFERENCE TEXT (built word for word from the source; headings and amendments use the "
             "fixed templates):"]
    for d in ctx["drafts"]:
        el = f"{d.citation_key}#{'TEXT' if d.part == 'HEAD' else d.part}"
        lines.append(f"  {el} [{d.kind}] {d.text}")
    for name, rows in (("MODEL_1", gemini_rows), ("MODEL_2", claude_rows)):
        if not rows:
            lines.append(f"{name}: FAIL - no readable CLUSTER|TYPE|ELEMENT|RESULTS table")
            continue
        per, table = _check_rows(rows, ctx)
        bad = {k: v for k, v in per.items() if v}
        lines.append(f"{name}: {len(per) - len(bad)} of {len(per)} MKPs PASS")
        for k, v in bad.items():
            lines.append(f"  {name} | {k} | FAIL | {'; '.join(v)}")
        for t in table:
            lines.append(f"  {name} | TABLE | FAIL | {t}")
    return "\n".join(lines)


# ---------------------------------------------------------------- word-list gate (CLAUDE.md §4)

def _word_list_check(text: str, ref_text: str, word_lists: dict) -> List[str]:
    """Return error strings for any modal verb, negation, qualifier, number or date present in
    the source (ref_text) but absent from the MKP text.  Checks are case-insensitive whole-word
    matches so 'shall' does not match 'marshalling'."""
    errors: List[str] = []
    ref_lower = ref_text.lower()
    text_lower = text.lower()

    for category in ("modal", "negation", "qualifier"):
        for phrase in word_lists.get(category, []):
            pat = r"(?<![a-z])" + re.escape(phrase.lower()) + r"(?![a-z])"
            if re.search(pat, ref_lower) and not re.search(pat, text_lower):
                errors.append(f"{category} '{phrase}' from source missing in MKP")

    num_pat = word_lists.get("number_pattern", _DEFAULT_NUMBER_PATTERN)
    for m in re.finditer(num_pat, ref_text):
        val = m.group().strip()
        if val and val not in text:
            errors.append(f"number '{val}' from source missing in MKP")

    date_pat = word_lists.get("date_pattern", _DEFAULT_DATE_PATTERN)
    for m in re.finditer(date_pat, ref_text, re.IGNORECASE):
        val = m.group().strip()
        if val and val.lower() not in text_lower:
            errors.append(f"date '{val}' from source missing in MKP")

    return errors


# ---------------------------------------------------------------- final gate (before the DB)

def _set_field(row: dict, name: str, value: str) -> None:
    old = clean(row["RESULTS"])
    if re.search(rf"(?i)(?:^|;\s*){name}\s*:", old):
        row["RESULTS"] = re.sub(rf"(?i)(?P<p>(?:^|;\s*){name}\s*:)\s*[^;]*", lambda m: m.group("p") + " " + value, old, count=1)
    else:
        row["RESULTS"] = old + f"; {name}: {value}"


def standard_gate(rows: List[dict], ctx: dict, fidelity_mode: str = "BLOCK",
                  word_lists: Optional[dict] = None) -> Tuple[List[dict], List[str]]:
    errors, warnings = [], []
    cluster = ctx["cluster"]["id"]
    expected = set(ctx["expected"])
    provisions = {p["key"]: p for p in ctx["provisions"]}
    shared = {s["owner"] for s in ctx["cluster"]["shared_amendments"]}
    types = {r["TYPE"] for r in rows}
    if not {"BATCH", "MKP", "SUM"} <= types or not types <= ROW_TYPES:
        errors.append("expected BATCH, MKP, SUM rows and only known row types")
    for r in rows:
        if r["CLUSTER"] != cluster:
            errors.append(f"row {r['ELEMENT']} has cluster {r['CLUSTER']}, wanted {cluster}")
            break
    for r in rows:
        if r["TYPE"] == "UNL" and r["ELEMENT"] not in expected:
            errors.append(f"source boundary includes unselected {r['ELEMENT']}")

    # heading and operative text of one node share citation_key#TEXT -> heading becomes #HEAD (structure only)
    mkps = [r for r in rows if r["TYPE"] == "MKP"]
    by_el: Dict[str, List[dict]] = {}
    for r in mkps:
        by_el.setdefault(r["ELEMENT"], []).append(r)
    for el, group in by_el.items():
        heads = [r for r in group if get_field(r, "knowledge_type").upper() == "HEADING"]
        if len(group) == 2 and len(heads) == 1 and el.endswith("#TEXT"):
            h = heads[0]
            new = el[:-len("#TEXT")] + "#HEAD"
            sp_rows = [s for s in rows if s["TYPE"] == "SP" and s["ELEMENT"] == el]
            # SP rows follow their MKP row: the first SP run after the heading row belongs to it
            pos, run = rows.index(h), []
            for s in rows[pos + 1:]:
                if s["TYPE"] != "SP" or s["ELEMENT"] != el:
                    break
                run.append(s)
            if run and len(run) < len(sp_rows):
                h["ELEMENT"] = new
                _set_field(h, "part", "HEAD")
                for s in run:
                    s["ELEMENT"] = new
                warnings.append(f"{el}: heading MKP stored as {new} (same node also has operative text)")

    seen, sp_groups = set(), {}
    for r in rows:
        if r["TYPE"] == "SP":
            sp_groups.setdefault(r["ELEMENT"], []).append(r)
    mkps = [r for r in rows if r["TYPE"] == "MKP"]
    acronyms = {}
    for r in mkps:
        el = r["ELEMENT"]
        if "#" not in el:
            errors.append(f"missing #part in {el}")
            continue
        citation, part = el.split("#", 1)
        if citation not in expected and not (citation in shared and part.startswith("AMD")) and part != "HEAD":
            errors.append(f"MKP outside the selection: {el}")
            continue
        if el in seen:
            errors.append(f"repeated MKP {el}")
            continue
        seen.add(el)
        if get_field(r, "citation_key") != citation or get_field(r, "part") != part:
            errors.append(f"citation_key/part do not match ELEMENT {el}")
        text = get_field(r, "text")
        if not text:
            errors.append(f"empty legal text {el}")
            continue
        phrases = sp_groups.get(el, [])
        if not phrases or " ".join(get_field(s, "text") for s in phrases) != text:
            errors.append(f"phrases do not rejoin to the text {el}")
        seq = [get_field(s, "sp") for s in phrases]
        if seq != [f"SP{n}" for n in range(1, len(seq) + 1)]:
            errors.append(f"SP sequence incomplete or duplicated {el}")
        concept = get_field(r, "concept")
        kt = get_field(r, "knowledge_type").upper()
        if not concept or not kt:
            errors.append(f"missing concept/knowledge_type {el}")
        else:
            acr = derive_id("M", concept)[2:]
            if len(acr) > 10:
                errors.append(f"concept acronym {acr} longer than 10 letters {el}")
            if acr in acronyms:
                errors.append(f"concept acronym {acr} used by {acronyms[acr]} and {el}")
            acronyms[acr] = el

        # parent from the IKP hierarchy, not from the model (structural metadata only)
        node = provisions.get(citation)
        if node and node["level"].replace("_", "") in CHILD_LEVELS:
            parent = ctx["legal_parents"].get(citation) or node["parent"]
            if parent and parent != "NA" and citation.startswith(parent + "_"):
                if get_field(r, "parent") != parent:
                    _set_field(r, "parent", parent)

        # FIDELITY against the Python reference built from the IKP source
        ref = _reference(ctx, citation, part, kt)
        if ref is None:
            if part == "TEXT" and kt != "HEADING":
                warnings.append(f"{el}: no Python reference text to compare")
        elif kt != "HEADING" and part != "HEAD":
            diff = fidelity(text, ref.text)
            if diff:
                msg = f"FIDELITY {el}: {diff}"
                if part == "TEXT" and fidelity_mode == "BLOCK":
                    errors.append(msg)
                else:
                    warnings.append(msg)
            # Word-list gate: modal verbs, negations, qualifiers, numbers, dates (CLAUDE.md §4)
            if word_lists and part == "TEXT":
                for wl_err in _word_list_check(text, ref.text, word_lists):
                    errors.append(f"WORD_LIST {el}: {wl_err}")

    covered = {r["ELEMENT"].split("#", 1)[0] for r in mkps}
    if expected - covered:
        errors.append(f"uncovered provisions {sorted(expected - covered)}")
    for key in expected:
        parts = {r["ELEMENT"].split("#", 1)[1] for r in mkps if r["ELEMENT"].startswith(key + "#")}
        status = provisions[key]["status"].upper()
        omitted = "OMIT" in status or re.search(r"\[\*{3,}\]", provisions[key]["source_text"])
        if parts and "TEXT" not in parts and not (any(p.startswith("AMD") for p in parts) and omitted):
            errors.append(f"{key} has no operative TEXT MKP and is not an omitted provision")
    orphans = set(sp_groups) - {r["ELEMENT"] for r in mkps}
    if orphans:
        errors.append(f"phrase rows without an MKP {sorted(orphans)}")
    got = {r["ELEMENT"] for r in mkps}
    for (ck, part) in ctx["references"]:
        if part.startswith("AMD") and f"{ck}#{part}" not in got:
            warnings.append(f"no amendment MKP {ck}#{part}")
    children = sum(clean(get_field(r, "parent")).upper() not in {"", "NA"} for r in mkps)
    for r in rows:
        if r["TYPE"] == "SUM":
            _set_field(r, "child_mkps", str(children))
    if errors:
        raise GateError(errors)
    return rows, warnings
