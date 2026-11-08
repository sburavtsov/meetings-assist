"""Optional step: collect items to discuss from private messages to the bot and assemble the meeting agenda.

Participants (the approver + agenda.contributors) send items to the bot in a private chat; every line is one item.
  /agenda  assemble the agenda with the AI and reply with it     /list  show the items     /clear  start a new list
Items live in <out dir>/agenda.json; every assembled agenda is also saved to <out dir>/agendas/<timestamp>.md.
Written against the AIAdapter / MessengerAdapter interfaces only; enabled with agenda.enabled in config.json.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import prompts
from adapters.base import AIAdapter, IncomingMessage, MessengerAdapter, QuotaExceeded

HELP = ("Присылайте темы для обсуждения, каждая строка — отдельный пункт.\n"
        "/agenda — собрать повестку\n/list — показать пункты\n/clear — начать новый список")
_BULLET = re.compile(r"^\s*(?:[-–—•*·]|\d+[.)])\s*")


def parse_items(text: str) -> list[str]:
    """One item per non-empty line; leading bullets / numbering are stripped."""
    return [b for line in text.splitlines() if (b := _BULLET.sub("", line).strip())]


class Agenda:
    def __init__(self, ai: AIAdapter, state_dir: Path, allowed: set[str], max_items: int, speakers: str, context: str):
        self.ai, self.dir, self.allowed, self.max = ai, state_dir, allowed, max_items
        self.speakers, self.context = speakers, context
        self.path = state_dir / "agenda.json"

    # ---- state ------------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text())["items"] if self.path.exists() else []

    def _save(self, items: list[dict]) -> None:
        self.dir.mkdir(exist_ok=True)
        self.path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2))

    # ---- operations (each returns the reply text) ---------------------------------------------
    def add(self, sender_id: str, name: str, text: str) -> str:
        new, items = parse_items(text), self._load()
        if not new:
            return HELP
        room = self.max - len(items)
        if room <= 0:
            return f"Список полон ({self.max} пунктов). Соберите повестку: /agenda или начните заново: /clear"
        taken = new[:room]
        items += [{"text": t, "from": name, "sender_id": sender_id, "at": time.strftime("%Y-%m-%d %H:%M")} for t in taken]
        self._save(items)
        skipped = f" (не поместилось: {len(new) - len(taken)})" if len(taken) < len(new) else ""
        return f"Добавлено: {len(taken)}{skipped}. Всего пунктов: {len(items)}. Собрать повестку — /agenda"

    def show(self) -> str:
        items = self._load()
        if not items:
            return "Список пуст. Пришлите темы для обсуждения."
        return "Пункты:\n" + "\n".join(f"{i}. {x['text']} ({x['from']})" for i, x in enumerate(items, 1))

    def clear(self) -> str:
        n = len(self._load())
        self._save([])
        return f"Список очищен ({n})."

    def assemble(self) -> str:
        items = self._load()
        if not items:
            return "Список пуст. Пришлите темы для обсуждения."
        listing = "\n".join(f"{i}. {x['text']}" + (f" ({x['from']})" if x.get("from") else "") for i, x in enumerate(items, 1))
        try:
            text = self.ai.generate_text(prompts.agenda(listing, self.speakers, self.context))
        except QuotaExceeded:
            return "Дневной лимит AI исчерпан. Список сохранён, повторите /agenda позже."
        except Exception as e:
            print(f"  agenda: assembling failed: {str(e)[:150]}", flush=True)
            return f"Не удалось собрать повестку ({str(e)[:100]}). Список сохранён, повторите /agenda."
        (self.dir / "agendas").mkdir(parents=True, exist_ok=True)
        (self.dir / "agendas" / f"{time.strftime('%Y%m%d-%H%M%S')}.md").write_text(text + "\n")
        return text + "\n\nСписок сохранён: /clear — начать новый."

    # ---- Telegram-style entry point -------------------------------------------------------------
    def answer(self, msg: IncomingMessage) -> str:
        if msg.sender_id not in self.allowed:
            return "Нет доступа."
        text = msg.text.strip()
        if text.startswith("/"):
            cmd = text.split()[0].split("@")[0].lower()
            return {"/agenda": self.assemble, "/list": self.show, "/clear": self.clear}.get(cmd, lambda: HELP)()
        return self.add(msg.sender_id, msg.sender_name, text)

    def handle(self, messenger: MessengerAdapter, messages: list[IncomingMessage]) -> None:
        for m in messages:
            try:
                messenger.reply(m, self.answer(m))
            except Exception as e:  # one bad message must not stop the others
                print(f"  agenda: failed to handle a message: {str(e)[:120]}", flush=True)
