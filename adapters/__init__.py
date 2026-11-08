"""Adapter registry: config.json names an adapter, this module loads it (lazily, so an unused adapter's
dependencies are never imported).

  ai.adapter        -> AIAdapter        messenger.adapter -> MessengerAdapter        tasks.adapter -> TaskAdapter
"""
from __future__ import annotations

import importlib
from pathlib import Path

REGISTRY = {
    "ai": {"google": "adapters.ai_google:GoogleAIAdapter"},
    "messenger": {"telegram": "adapters.messenger_telegram:TelegramAdapter"},
    "tasks": {"yougile": "adapters.tasks_yougile:YougileAdapter"},
}


def _class(kind: str, name: str):
    try:
        path = REGISTRY[kind][name]
    except KeyError:
        raise RuntimeError(f"unknown {kind} adapter {name!r}; available: {sorted(REGISTRY[kind])}") from None
    mod, cls = path.split(":")
    return getattr(importlib.import_module(mod), cls)


def create_ai(section: dict):
    return _class("ai", section["adapter"])(section)


def create_messenger(section: dict, state_dir: Path):
    return _class("messenger", section["adapter"])(section, state_dir)


def create_tasks(section: dict):
    return _class("tasks", section["adapter"])(section)
