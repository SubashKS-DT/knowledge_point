"""What you see in the command prompt, and the full detail in Output/logs.

say(...)   important line: shown on screen AND written to the log
log(...)   detail (model calls, candidates, scores): written to the log only
The log file is Output/logs/law_arc_<date>_<time>.log, one per run.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_log_path: Optional[Path] = None
VERBOSE = False            # tests may set True to see everything on screen


def _file() -> Optional[Path]:
    global _log_path
    if _log_path is None:
        try:
            from kp_settings import build_paths
            folder = build_paths()["log_dir"]
        except Exception:
            folder = Path(__file__).resolve().parent / "Output" / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _log_path = folder / f"law_arc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        except Exception:
            return None
    return _log_path


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    if VERBOSE:
        print(text)
    path = _file()
    if path is not None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass


def say(*parts) -> None:
    text = " ".join(str(p) for p in parts)
    print(text)
    sys.stdout.flush()
    if not VERBOSE:
        path = _file()
        if path is not None:
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except Exception:
                pass


def log_path() -> str:
    p = _file()
    return str(p) if p else ""


def show_change(label: str, old: str, new: str, width: int = 400) -> None:
    """When the LLM is called because a text changed: show both versions."""
    say(f"  {label} v1 : {' '.join(str(old).split())[:width]}")
    say(f"  {label} v2 : {' '.join(str(new).split())[:width]}")
