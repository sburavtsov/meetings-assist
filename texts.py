"""User-facing texts of the bot and the follow-up message: defaults/messages/<language>.json, overridable per key in
~/.meetings-assist/messages/<language>.json. Language: messenger.language in config.json."""
from __future__ import annotations

import settings

_cache: dict = {}


def _all() -> dict:
    lang = settings.S["messenger"].get("language") or "ru"
    if _cache.get("lang") != lang:
        _cache.clear()
        _cache.update(lang=lang, msgs=settings.layered_json(f"messages/{lang}.json"))
    return _cache["msgs"]


def reset() -> None:
    """Forget the cached texts (settings were reloaded)."""
    _cache.clear()


def t(key: str, **kw) -> str:
    s = _all()[key]
    return s.format(**kw) if kw else s


def validate() -> int:
    """Every placeholder used by the code must exist in the texts; returns the number of texts."""
    msgs = _all()
    missing = sorted(set(settings.layered_json("messages/ru.json")) - set(msgs))
    if missing:
        raise RuntimeError(f"messages: missing keys {missing}")
    return len(msgs)
