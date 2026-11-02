"""GKP / membership decisions: two verifiers in parallel, judge only on disagreement.

  verifier 1 (Gemini) + verifier 2 (Claude), same prompt (MODE VERIFY), every (atom, subgroup) pair
  both TRUE  -> APPROVED (method AGREE)        both FALSE -> not a member
  otherwise  -> judge (MODE JUDGE) sees both verdicts + reasons: TRUE -> APPROVED (JUDGE), FALSE -> not a member,
                UNCERTAIN / no answer -> REVIEW
Missing pairs are re-asked (max_attempts); an omitted pair is never read as FALSE.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple


def parse_json(raw: str, key: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.I)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("response has no JSON object")
    data = json.loads(text[a:b + 1])
    if not isinstance(data.get(key), list):
        raise ValueError(f"JSON has no '{key}' array")
    return data


def collect(decision: dict, expected: Dict[str, set], accepted: dict) -> Tuple[dict, list]:
    """Keep valid, single answers per (element, subgroup); TRUE / UNCERTAIN need a reason."""
    seen = defaultdict(list)
    for item in decision.get("atoms", []):
        if not isinstance(item, dict):
            continue
        el = str(item.get("element", "")).strip()
        if el not in expected or not isinstance(item.get("assignments"), list):
            continue
        for m in item["assignments"]:
            if isinstance(m, dict) and str(m.get("tsg_id", "")).strip() in expected[el]:
                seen[(el, str(m["tsg_id"]).strip())].append(m)
    for key, items in seen.items():
        if key in accepted or len(items) != 1:
            continue
        v = str(items[0].get("verdict", "")).upper().strip()
        reason = items[0].get("reason", "") if isinstance(items[0].get("reason", ""), str) else ""
        if v not in ("TRUE", "FALSE", "UNCERTAIN") or (v != "FALSE" and not reason.strip()):
            continue
        accepted[key] = {"verdict": v, "reason": reason.strip()}
    missing = [(el, sid) for el, ids in expected.items() for sid in ids if (el, sid) not in accepted]
    return accepted, missing


class Verifier:
    def __init__(self, call: Callable[[str, str, str], str], prompt: str, models: dict, max_attempts: int, log=print):
        self.call, self.prompt, self.models, self.max_attempts, self.log = call, prompt, models, max_attempts, log

    def _payload(self, mode, atoms, ctx, extra=None):
        p = {"mode": mode, **ctx, "atoms": atoms}
        if extra:
            p.update(extra)
        return self.prompt + "\n\nINPUT JSON:\n" + json.dumps(p, ensure_ascii=False, default=str)

    def _ask(self, provider: str, model: str, atoms: List[dict], ctx: dict, phase: str) -> dict:
        expected = {a["element"]: {c["tsg_id"] for c in a["candidates"]} for a in atoms}
        accepted: dict = {}
        for attempt in range(1, self.max_attempts + 1):
            pending = [dict(a, candidates=[c for c in a["candidates"] if (a["element"], c["tsg_id"]) not in accepted])
                       for a in atoms]
            pending = [a for a in pending if a["candidates"]]
            if not pending:
                break
            try:
                raw = self.call(provider, model, self._payload("VERIFY", pending, dict(ctx, phase=phase)))
                accepted, missing = collect(parse_json(raw, "atoms"), expected, accepted)
            except Exception as exc:
                self.log(f"VERIFY {provider:<6} : {type(exc).__name__}: {str(exc)[:140]} (attempt {attempt})")
                continue
            if not missing:
                break
        return accepted

    def decide(self, atoms: List[dict], ctx: dict, phase: str) -> Dict[Tuple[str, str], dict]:
        """-> {(element, tsg_id): {status: APPROVED|REVIEW|REJECTED, method, model_1, model_2, reason}}"""
        atoms = [a for a in atoms if a.get("candidates")]
        if not atoms:
            return {}
        m = self.models
        pairs = sum(len(a["candidates"]) for a in atoms)
        self.log(f"MODELS                 : {phase} / {len(atoms)} atoms, {pairs} pairs - {m['verifier_1']['model']} + {m['verifier_2']['model']}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self._ask, m["verifier_1"]["llm"], m["verifier_1"]["model"], atoms, ctx, phase)
            f2 = pool.submit(self._ask, m["verifier_2"]["llm"], m["verifier_2"]["model"], atoms, ctx, phase)
            v1, v2 = f1.result(), f2.result()
        out, disputed = {}, defaultdict(list)
        for a in atoms:
            for c in a["candidates"]:
                k = (a["element"], c["tsg_id"])
                d1, d2 = v1.get(k), v2.get(k)
                a1, a2 = (d1 or {}).get("verdict"), (d2 or {}).get("verdict")
                base = {"model_1": a1, "model_2": a2}
                if a1 == a2 == "TRUE":
                    out[k] = dict(base, status="APPROVED", method="AGREE", reason=(d2 or d1)["reason"])
                elif a1 == a2 == "FALSE":
                    out[k] = dict(base, status="REJECTED", method="AGREE", reason="")
                else:
                    disputed[a["element"]].append(dict(c, model_1={"verdict": a1, "reason": (d1 or {}).get("reason", "")},
                                                       model_2={"verdict": a2, "reason": (d2 or {}).get("reason", "")}))
                    out[k] = dict(base, status="REVIEW", method="JUDGE", reason="verifiers disagree; judge verdict missing")
        if disputed:
            jatoms = [dict(next(a for a in atoms if a["element"] == el), candidates=cs) for el, cs in disputed.items()]
            self.log(f"JUDGE                  : {sum(len(c) for c in disputed.values())} disputed pairs - {m['judge']['model']}")
            judged = self._judge(jatoms, ctx, phase)
            for k, j in judged.items():
                st = {"TRUE": "APPROVED", "FALSE": "REJECTED"}.get(j["verdict"], "REVIEW")
                out[k] = dict(out[k], status=st, method="JUDGE", reason=j["reason"])
        return out

    def _judge(self, atoms, ctx, phase) -> dict:
        expected = {a["element"]: {c["tsg_id"] for c in a["candidates"]} for a in atoms}
        accepted: dict = {}
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self.call(self.models["judge"]["llm"], self.models["judge"]["model"],
                                self._payload("JUDGE", atoms, dict(ctx, phase=phase)))
                accepted, missing = collect(parse_json(raw, "atoms"), expected, accepted)
                if not missing:
                    break
            except Exception as exc:
                self.log(f"JUDGE                  : {type(exc).__name__}: {str(exc)[:140]} (attempt {attempt})")
        return accepted


def parse_proposals(raw: str, eligible: List[dict]) -> List[dict]:
    """TG18 proposals are recommendations only; evidence must appear in the atom's own text."""
    data = parse_json(raw, "proposals")
    indexed = {a["element"]: a for a in eligible}
    out, seen = [], set()
    for item in data["proposals"]:
        if not isinstance(item, dict):
            continue
        el = str(item.get("element", "")).strip()
        if el not in indexed or el in seen or item.get("action") != "PROPOSE":
            continue
        seen.add(el)
        name = " ".join(str(item.get("name", "")).split())
        definition = " ".join(str(item.get("definition", "")).split())
        inc, exc = item.get("include"), item.get("exclude")
        evidence = " ".join(str(item.get("evidence", "")).split())
        reason = " ".join(str(item.get("reason", "")).split())
        if not (3 <= len(name) <= 85 and 20 <= len(definition) <= 550 and isinstance(inc, list) and isinstance(exc, list)
                and inc and exc and all(isinstance(x, str) and x.strip() for x in inc + exc) and evidence and reason):
            continue
        source = " ".join(str(indexed[el].get(k, "")) for k in ("concept", "results", "local_results", "family"))
        if re.sub(r"\s+", " ", evidence).casefold() not in re.sub(r"\s+", " ", source).casefold():
            continue
        out.append({"element": el, "proposal_id": "TSG18_P" + hashlib.sha256(name.casefold().encode()).hexdigest()[:10].upper(),
                    "name": name, "definition": definition, "include": inc, "exclude": exc, "evidence": evidence, "reason": reason})
    return out
