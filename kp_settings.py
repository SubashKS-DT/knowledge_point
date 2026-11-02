"""LAW ARC settings: permission.json (your switches) + Config/settings.json (fixed settings) + the Law folder.

permission.json   run / use_cache per step, which kinds of law to read, scope, governance master choice, and the
                  LLM + model of every role ({"llm": "GEMINI" | "CLAUDE", "model": "..."}).
Config/settings.json  paths, table names, file names, thresholds, word lists (rarely changed).
Law/Acts, Law/Rules, Law/Notifications, Law/Circulars
                  every PDF in a folder whose kind is "read": true in permission.json is processed. Its name, code
                  and type come from Config/documents.json (keyed by file name); a new PDF is added there
                  automatically, with a code made from its file name.

load() returns one dict with the sections the engines read: paths, files, documents, ikp, mkp, akp, gkp,
governance_master, vector.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
PERMISSION_FILE = BASE_DIR / "permission.json"
SETTINGS_FILE = BASE_DIR / "Config" / "settings.json"
STEPS = ("ikp", "mkp", "akp", "gkp", "governance_master")
LLMS = ("GEMINI", "CLAUDE")
ROLES = {"mkp": ("generator_1", "generator_2", "judge"),
         "akp": ("generator_1", "generator_2", "judge", "comparison", "same"),
         "gkp": ("verifier_1", "verifier_2", "judge", "proposer")}
TIERS = ("SIMPLE", "COMPLEX")

_cache: Optional[dict] = None


class SettingsError(ValueError):
    pass


def _read(path: Path) -> dict:
    if not path.is_file():
        raise SettingsError(f"File not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SettingsError(f"{path.name} must contain a JSON object")
    return data


def resolve(value: str) -> Path:
    p = Path(str(value).strip())
    return p if p.is_absolute() else BASE_DIR / p


def _role(value, where: str) -> dict:
    if not isinstance(value, dict) or not value.get("llm") or not value.get("model"):
        raise SettingsError(f'permission.json {where} must be {{"llm": "GEMINI" or "CLAUDE", "model": "..."}}')
    llm = str(value["llm"]).strip().upper()
    if llm not in LLMS:
        raise SettingsError(f"permission.json {where}.llm must be GEMINI or CLAUDE (got {value['llm']})")
    return {"llm": llm, "model": str(value["model"]).strip()}


def _models(perm: dict, step: str) -> dict:
    block = (perm.get("llm") or {}).get(step) or {}
    out = {}
    for tier in TIERS:
        t = block.get(tier) or {}
        out[tier] = {role: _role(t.get(role), f"llm.{step}.{tier}.{role}") for role in ROLES[step]}
    return out


# ---------------------------------------------------------------- documents from the Law folder

def _derive(file_name: str, doc_type: str) -> dict:
    """Name and code for a PDF that is not in Config/documents.json yet (from its file name)."""
    stem = Path(file_name).stem
    words = re.sub(r"[_\-]+", " ", stem).strip()
    nums = re.findall(r"\d+", stem)
    if doc_type == "NOTIFICATION":
        n, y = (nums + ["0", "0"])[:2]
        rest = re.sub(r"^\s*\d+\s*\d+\s*", "", words).strip()
        rest = re.sub(r"\bRate\b", "(Rate)", rest)
        return {"name": f"Notification {int(n)}/{y}" + (f"-{rest}" if rest else ""), "code": f"N{int(n):02d}_{y}"}
    if doc_type == "CIRCULAR":
        return {"name": "Circular " + "/".join(nums[:3]) + (" - " + re.sub(r"(?i)^\s*circular[\s\d]*", "", words).strip()
                                                             if re.sub(r"(?i)^\s*circular[\s\d]*", "", words).strip() else ""),
                "code": "C" + "_".join(nums[:3])}
    year = next((y[-2:] for y in nums if len(y) == 4), "")
    head = re.split(r"(?i)\b(act|rules?)\b", words)[0].strip()
    letters = "".join(w if w.isupper() else w[0].upper() for w in head.split() if w)
    part = re.search(r"(?i)\bpart\s*([A-Z])\b", words)
    if doc_type == "RULES":
        return {"name": f"{head} Rules".strip() + (f" Part {part.group(1).upper()}" if part else ""), "code": f"{letters}R{year}" + (part.group(1).upper() if part and part.group(1).upper() != "A" else "")}
    return {"name": f"{head} Act".strip(), "code": f"{letters}A{year}"}


def guess_type(file_name: str) -> str:
    """ACT / RULES / NOTIFICATION / CIRCULAR from the file name; "" when the name does not say."""
    n = file_name.lower()
    if "circular" in n:
        return "CIRCULAR"
    if "notification" in n or re.search(r"(central|integrated|union territory|state)[ _-]*tax", n):
        return "NOTIFICATION"
    if re.search(r"(^|[^a-z])rules?([^a-z]|$)", n):
        return "RULES"
    if re.search(r"(^|[^a-z])act([^a-z]|$)", n):
        return "ACT"
    return ""


def discover_documents(perm: dict, settings: dict, write_registry: bool = True) -> Dict[str, dict]:
    """{code: {name, code, file, type, read}} for every PDF under the Law kind folders."""
    law_dir = resolve(settings["paths"]["law_dir"])
    reg_path = resolve(settings["files"]["documents_registry"])
    registry = _read(reg_path) if reg_path.is_file() else {}
    wanted = {k: bool(v) for k, v in (perm.get("read") or {}).items()}
    unknown = set(wanted) - set(settings["law_folders"])
    if unknown:
        raise SettingsError(f"permission.json read: unknown kind(s) {sorted(unknown)}; use {sorted(settings['law_folders'])}")
    docs: Dict[str, dict] = {}
    added = False
    # PDFs left directly in Law/ (old layout): move the ones whose kind is known into their sub-folder
    by_type = {spec["type"]: spec["folder"] for spec in settings["law_folders"].values()}
    if law_dir.is_dir():
        for pdf in sorted(law_dir.glob("*.pdf")):
            info = registry.get(pdf.name)
            folder = by_type.get(str((info or {}).get("type", "") or guess_type(pdf.name)).upper())
            if folder:
                (law_dir / folder).mkdir(exist_ok=True)
                if not (law_dir / folder / pdf.name).exists():
                    pdf.rename(law_dir / folder / pdf.name)
                    print(f"LAW FOLDER : moved {pdf.name} -> Law/{folder}/")
            else:
                print(f"LAW FOLDER : {pdf.name} is directly in Law/ - move it into Law/Acts, Law/Rules, "
                      "Law/Notifications or Law/Circulars so it can be read")
    for kind, spec in settings["law_folders"].items():
        folder = law_dir / spec["folder"]
        if not folder.is_dir():
            continue
        for pdf in sorted(folder.glob("*.pdf"), key=lambda p: p.name.lower()):
            info = registry.get(pdf.name)
            if not isinstance(info, dict):
                info = {**_derive(pdf.name, spec["type"]), "type": spec["type"]}
                registry[pdf.name] = info
                added = True
            code = str(info["code"]).strip().upper()
            if code in docs:
                raise SettingsError(f"Two PDFs have the code {code}: {docs[code]['file']} and {spec['folder']}/{pdf.name} - "
                                    f"give one a different code in {reg_path.name}")
            docs[code] = {"name": info["name"], "code": code, "file": f"{spec['folder']}/{pdf.name}",
                          "type": str(info.get("type") or spec["type"]).upper(), "read": wanted.get(kind, False)}
    if added and write_registry:
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return docs


# ---------------------------------------------------------------- one merged config

def load(fresh: bool = False, permission: Optional[dict] = None, settings: Optional[dict] = None) -> dict:
    global _cache
    if _cache is not None and not fresh and permission is None and settings is None:
        return copy.deepcopy(_cache)
    perm = permission if permission is not None else _read(PERMISSION_FILE)
    st = settings if settings is not None else _read(SETTINGS_FILE)
    run, cache = perm.get("run") or {}, perm.get("use_cache") or {}
    for name, block in (("run", run), ("use_cache", cache)):
        bad = [s for s in STEPS if type(block.get(s)) is not bool]
        if bad:
            raise SettingsError(f"permission.json {name}: set {', '.join(bad)} to true or false")
    scope = perm.get("scope") or {}
    gm = perm.get("governance_master") or {}
    llm = perm.get("llm") or {}
    cfg = {"paths": dict(st["paths"]), "files": dict(st["files"]), "law_folders": st["law_folders"]}
    ikp_role = _role(llm.get("ikp"), "llm.ikp")
    cfg["ikp"] = {**st["ikp"], "enabled": run["ikp"], "use_cache": cache["ikp"],
                  "llm": {"mode": st["ikp"].get("llm_mode", "ON_FAIL"), "provider": ikp_role["llm"],
                          "model": ikp_role["model"], "chapters": st["ikp"].get("llm_chapters", [])}}
    cfg["mkp"] = {**st["mkp"], "enabled": run["mkp"], "use_cache": cache["mkp"],
                  "ask_user_input": bool(scope.get("ask_user_input", True)), "scope": scope.get("default", "FULL"),
                  "models": _models(perm, "mkp")}
    cfg["akp"] = {**st["akp"], "enabled": run["akp"], "use_cache": cache["akp"], "models": _models(perm, "akp")}
    cfg["gkp"] = {**st["gkp"], "enabled": run["gkp"], "use_cache": cache["gkp"], "models": _models(perm, "gkp")}
    cfg["governance_master"] = {**st["governance_master"], "enabled": run["governance_master"],
                                "use_cache": cache["governance_master"], "tsgs": gm.get("tsgs", "TOUCHED"),
                                "law_as_on": str(gm.get("law_as_on", "") or "")}
    emb = llm.get("embedding") or {}
    cfg["vector"] = {**st["vector"], "embedding_model": str(emb.get("model") or "text-embedding-005")}
    cfg["documents"] = discover_documents(perm, st)
    if permission is None and settings is None:
        _cache = cfg
    return copy.deepcopy(cfg)


def build_paths(cfg: Optional[dict] = None) -> dict:
    """Absolute paths used by every step (folders, credentials, prompts)."""
    cfg = cfg or load()
    p, f = cfg["paths"], cfg["files"]
    out = {k: resolve(v) for k, v in p.items()}
    cred = out["credentials_dir"]
    out.update({"env_file": cred / f["env_file"], "gemini_credentials": cred / f["gemini_credentials"],
                "claude_credentials": cred / f["claude_credentials"]})
    for k, v in f.items():
        if k.endswith("_prompt"):
            out[k] = out["prompt_dir"] / v
    out["output_dir"].mkdir(parents=True, exist_ok=True)
    return out


def selected_documents(cfg: Optional[dict] = None) -> List[dict]:
    cfg = cfg or load()
    return [dict(d, document_key=code) for code, d in cfg["documents"].items() if d.get("read")]
