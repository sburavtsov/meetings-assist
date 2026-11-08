"""Secrets live outside the project, in ~/.meetings-assist/secrets.json (dir 700, file 600).

  {"GEMINI_API_KEY": "...", "YOUGILE_API_KEY": "...", "TELEGRAM_BOT_TOKEN": "..."}

get(name)  -> value from that file; an environment variable of the same name overrides it.
set(name, value) -> writes it back (used by yougile_setup_key.py).
Stdlib only, works on system Python 3.9 as well as uv's.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PATH = Path(os.environ.get("MEETINGS_ASSIST_SECRETS", Path.home() / ".meetings-assist" / "secrets.json"))


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{PATH} is not valid JSON: {e}") from e


def get(name: str, required: bool = True) -> str:
    value = os.environ.get(name) or _load().get(name, "")
    if required and not value:
        raise RuntimeError(f"{name} is missing: add it to {PATH}")
    return value


def set(name: str, value: str) -> None:  # noqa: A001
    PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = _load()
    data[name] = value
    tmp = PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PATH)
    PATH.chmod(0o600)
