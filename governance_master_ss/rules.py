"""GOVERNANCE MASTER / rules: clean AKP rules into the engine's expression grammar and check them.

The engine grammar (decision_graph/expr/grammar.lark):
    expr   := or ; or := and ("OR" and)* ; and := not ("AND" not)* ; not := "NOT" not | atom
    atom   := "(" expr ")" | ID CMP literal | ID "IN" "[" literal ("," literal)* "]" | ID "IS_SET" | ID "IS_EMPTY" | ID
    literal:= 'text' | number | TRUE | FALSE | DATE 'YYYY-MM-DD' ;  CMP := = != > >= < <=
A bare ID is a node (predicate / group / product); an ID followed by an operator is a fact.
This module re-implements that grammar so GOVERNANCE MASTER can check every rule before the engine sees it.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

KEYWORDS = ("AND", "OR", "NOT", "IN", "IS_SET", "IS_EMPTY", "TRUE", "FALSE", "DATE")
_SPEC = [("WS", r"\s+"), ("STRING", r"'(?:[^']|'')*'"), ("NUMBER", r"-?[0-9]+(?:\.[0-9]+)?(?![0-9A-Za-z_.])"),
         ("CMP", r"!=|>=|<=|=|>|<"), ("LPAR", r"\("), ("RPAR", r"\)"), ("LSQB", r"\["), ("RSQB", r"\]"),
         ("COMMA", r","), ("WORD", r"[A-Z][A-Z0-9_]*")]
_RX = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _SPEC))
ID_TOKEN = re.compile(r"\b[A-Z][A-Z0-9_]*\b")


class RuleError(ValueError):
    pass


def tokens(text: str) -> List[Tuple[str, str]]:
    pos, out = 0, []
    while pos < len(text):
        m = _RX.match(text, pos)
        if not m:
            raise RuleError(f"unexpected character at {pos + 1}: {text[pos:pos + 12]!r}")
        kind, val = m.lastgroup, m.group()
        if kind != "WS":
            out.append((val if kind == "WORD" and val in KEYWORDS else ("ID" if kind == "WORD" else kind), val))
        pos = m.end()
    return out + [("$END", "")]


def parse(text: str) -> Dict[str, Set[str]]:
    """Check `text` against the grammar. Returns {'facts': ids used as facts, 'nodes': bare ids}."""
    toks, i = tokens(text), [0]
    facts, nodes = set(), set()

    def peek():
        return toks[i[0]][0]

    def take(kind=None):
        k, v = toks[i[0]]
        if kind and k != kind:
            raise RuleError(f"expected {kind} but found {v or 'end of rule'!r} in {text!r}")
        i[0] += 1
        return v

    def literal():
        k = peek()
        if k in ("STRING", "NUMBER", "TRUE", "FALSE"):
            take()
        elif k == "DATE":
            take()
            if not re.fullmatch(r"'\d{4}-\d{2}-\d{2}'", take("STRING")):
                raise RuleError(f"DATE must be 'YYYY-MM-DD' in {text!r}")
        else:
            raise RuleError(f"expected a value but found {toks[i[0]][1] or 'end'!r} in {text!r}")

    def atom():
        if peek() == "LPAR":
            take(); orx(); take("RPAR"); return
        ident = take("ID")
        k = peek()
        if k == "CMP":
            take(); literal(); facts.add(ident)
        elif k == "IN":
            take(); take("LSQB"); literal()
            while peek() == "COMMA":
                take(); literal()
            take("RSQB"); facts.add(ident)
        elif k in ("IS_SET", "IS_EMPTY"):
            take(); facts.add(ident)
        else:
            nodes.add(ident)

    def notx():
        if peek() == "NOT":
            take(); notx()
        else:
            atom()

    def andx():
        notx()
        while peek() == "AND":
            take(); notx()

    def orx():
        andx()
        while peek() == "OR":
            take(); andx()

    if not text or not text.strip():
        raise RuleError("empty rule")
    orx()
    if peek() != "$END":
        raise RuleError(f"unexpected {toks[i[0]][1]!r} in {text!r}")
    return {"facts": facts, "nodes": nodes}


def clean(rule: str, bool_facts: Set[str], aliases: Dict[str, str]) -> str:
    """Typical AKP spellings -> engine grammar. Never changes meaning:
    "x" -> 'x' ; == -> = ; <> -> != ; && / || -> AND / OR ; IS NOT NULL -> IS_SET ; IS NULL -> IS_EMPTY ;
    F NOT IN [..] -> NOT F IN [..] ; BOOLEAN fact = 'TRUE' / 'YES' -> = TRUE ; fact ids renamed by `aliases`."""
    r = (rule or "").strip()
    r = re.sub(r'"([^"]*)"', lambda m: "'" + m.group(1).replace("'", "''") + "'", r)
    r = r.replace("==", "=").replace("<>", "!=").replace("&&", " AND ").replace("||", " OR ")
    r = re.sub(r"\b(and|or|not|in|true|false)\b", lambda m: m.group(1).upper(), r)
    r = re.sub(r"\bIS\s+NOT\s+NULL\b", "IS_SET", r, flags=re.I)
    r = re.sub(r"\bIS\s+NULL\b", "IS_EMPTY", r, flags=re.I)
    r = re.sub(r"\b([A-Z][A-Z0-9_]*)\s+NOT\s+IN\s*\[", r"NOT \1 IN [", r)
    r = ID_TOKEN.sub(lambda m: aliases.get(m.group(0), m.group(0)), r)
    for f in bool_facts:
        r = re.sub(rf"\b{re.escape(f)}\s*(=|!=)\s*'(TRUE|YES|Y)'", rf"{f} \1 TRUE", r, flags=re.I)
        r = re.sub(rf"\b{re.escape(f)}\s*(=|!=)\s*'(FALSE|NO|N)'", rf"{f} \1 FALSE", r, flags=re.I)
    return re.sub(r"\s+", " ", r).strip()


def rename(expr: str, mapping: Dict[str, str]) -> str:
    """Rename node / fact ids inside an expression, leaving quoted strings alone."""
    parts = re.split(r"('(?:[^']|'')*')", expr or "")
    return "".join(p if p.startswith("'") else ID_TOKEN.sub(lambda m: mapping.get(m.group(0), m.group(0)), p)
                   for p in parts)


def check(rule: str, known_facts: Set[str], known_nodes: Optional[Set[str]] = None) -> List[str]:
    """Problems with `rule` (empty list = fine): grammar, unknown facts, unknown nodes."""
    try:
        used = parse(rule)
    except RuleError as exc:
        return [str(exc)]
    errs = [f"unknown fact {f}" for f in sorted(used["facts"] - known_facts)]
    if known_nodes is not None:
        errs += [f"unknown node {n}" for n in sorted(used["nodes"] - known_nodes)]
    elif used["nodes"]:
        errs.append(f"a rule may only test facts, found {sorted(used['nodes'])}")
    return errs


def values_used(rule: str) -> Dict[str, Set[str]]:
    """{fact: {'TEXT', ...}} for every quoted value a rule compares a fact with (= / != / IN [...])."""
    out: Dict[str, Set[str]] = {}
    try:
        toks = tokens(rule)
    except RuleError:
        return out
    i = 0
    while i < len(toks):
        k, v = toks[i]
        if k == "ID" and i + 1 < len(toks):
            nk = toks[i + 1][0]
            if nk == "CMP" and i + 2 < len(toks) and toks[i + 2][0] == "STRING":
                out.setdefault(v, set()).add(toks[i + 2][1][1:-1].replace("''", "'"))
            elif nk == "IN":
                j = i + 3
                while j < len(toks) and toks[j][0] != "RSQB":
                    if toks[j][0] == "STRING":
                        out.setdefault(v, set()).add(toks[j][1][1:-1].replace("''", "'"))
                    j += 1
        i += 1
    return out
