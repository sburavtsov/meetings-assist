"""Layered settings: built-in defaults (./defaults/, next to the scripts) + your overrides (~/.meetings-assist/).

  defaults/config.json        every setting with its default value
  defaults/prompts/*.txt      AI prompts
  defaults/cards/<tasks adapter>.json   card layout for the task system
The same relative paths in ~/.meetings-assist/ override the defaults, per file:
  config.json   only the keys you want to change (deep merge; unknown keys are reported)
  prompts/X.txt replaces that one prompt
  cards/X.json  replaces the named top-level cards ("meeting" / "task"), the rest keep their defaults
Secrets are separate: secrets_config.py. Stdlib only.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULTS_DIR = ROOT / "defaults"
USER_CONFIG = Path(os.environ.get("MEETINGS_ASSIST_CONFIG", Path.home() / ".meetings-assist" / "config.json"))
USER_DIR = USER_CONFIG.parent


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{p} is not valid JSON: {e}") from e


def _merge(base: dict, over: dict, where: str = "") -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k not in base:
            print(f"settings: unknown key {where}{k!r} in {USER_CONFIG} (ignored)", file=sys.stderr)
        elif isinstance(base[k], dict) and isinstance(v, dict) and base[k]:
            out[k] = _merge(base[k], v, f"{where}{k}.")
        else:                                  # scalars, lists and free-form maps (default {}) are replaced
            out[k] = v
    return out


def load() -> dict:
    base = _read_json(DEFAULTS_DIR / "config.json")
    user = _read_json(USER_CONFIG) if USER_CONFIG.exists() else {}
    return _merge(base, user)


S = load()


def get(dotted: str):
    v = S
    for part in dotted.split("."):
        v = v[part]
    return v


def require(dotted: str) -> str:
    """A setting that has no sensible default: fail with a message that says where to set it."""
    v = get(dotted)
    if v in ("", None):
        raise RuntimeError(f"setting '{dotted}' is not set: add it to {USER_CONFIG}")
    return str(v)


def watch_dir() -> Path:
    return Path(require("watch.dir")).expanduser()


def out_dir() -> Path:
    return Path(S["watch"]["out_dir"]).expanduser() if S["watch"]["out_dir"] else watch_dir() / "transcripts"


def text(name: str) -> str:
    """Prompt/text file: ~/.meetings-assist/<name> if it exists, else defaults/<name>."""
    p = USER_DIR / name
    return (p if p.exists() else DEFAULTS_DIR / name).read_text()


def layered_json(name: str) -> dict:
    """defaults/<name> with the top-level keys of ~/.meetings-assist/<name> (if any) replacing the defaults."""
    base = _read_json(DEFAULTS_DIR / name)
    p = USER_DIR / name
    if not p.exists():
        return base
    over = _read_json(p)
    for k in over:
        if k not in base:
            print(f"settings: unknown key {k!r} in {p} (ignored)", file=sys.stderr)
    return {**base, **{k: v for k, v in over.items() if k in base}}


def export_defaults() -> list[tuple[Path, bool]]:
    """Copy defaults/prompts and defaults/cards into ~/.meetings-assist/ as editable starting points (never overwrites)."""
    done = []
    for src in sorted(DEFAULTS_DIR.rglob("*")):
        rel = src.relative_to(DEFAULTS_DIR)
        if src.is_file() and rel.parts[0] in ("prompts", "cards"):
            dst = USER_DIR / rel
            fresh = not dst.exists()
            if fresh:
                dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            done.append((dst, fresh))
    return done
