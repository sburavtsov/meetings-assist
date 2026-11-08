"""Optional step: collect items to discuss from private messages to the bot and assemble the meeting agenda.

Participants (the approver + agenda.contributors) send items to the bot in a private chat; every line is one item.
  /agenda        assemble the agenda with the AI and reply with it (as often as you like; the list is kept)
  /agenda_add    add items (plain text works too)       /agenda_list   show the items
  /agenda_del 1 3 5-7  remove several items at once (bare /agenda_del: the next message is the numbers)
  /agenda_clear  start a new list                       /agenda_help
  (the old short names /list /del /clear /help /start still work). The table is COMMANDS below; it also
  drives the bot's command menu (`meetings.py bot-commands`).
Adding items never calls the AI: the list is a cache, and assembling is a separate call. With
agenda.include_open_questions the open questions of the newest analysed meeting are added at assembly time
(not stored in the list) as carry-over points.
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

# (name, description shown in the bot's menu). Names: lowercase letters, digits, underscore (Telegram's rule).
COMMANDS = [
    ("agenda", "Собрать повестку из списка (можно повторять)"),
    ("agenda_add", "Добавить пункты (можно и просто текстом)"),
    ("agenda_list", "Показать пункты и вопросы с прошлой встречи"),
    ("agenda_del", "Удалить пункты: /agenda_del 1 3 5-7"),
    ("agenda_clear", "Начать новый список"),
    ("agenda_help", "Как это работает"),
]
ALIASES = {"list": "agenda_list", "del": "agenda_del", "clear": "agenda_clear", "help": "agenda_help", "start": "agenda_help"}
HELP = ("Присылайте темы для обсуждения, каждая строка — отдельный пункт.\n"
        + "\n".join(f"/{n} — {d}" for n, d in COMMANDS))
_BULLET = re.compile(r"^\s*(?:[-–—•*·]|\d+[.)])\s*")
_NUMBERS = re.compile(r"^[\d\s,;.\-–—]+$")
_RANGE = re.compile(r"(\d+)(?:[-–—](\d+))?")


def parse_items(text: str) -> list[str]:
    """One item per non-empty line; leading bullets / numbering are stripped."""
    return [b for line in text.splitlines() if (b := _BULLET.sub("", line).strip())]


def parse_numbers(text: str) -> list[int] | None:
    """'1, 2 3; 5-7' -> [1, 2, 3, 5, 6, 7]. None if the text is not purely item numbers."""
    text = text.strip()
    if not text or not _NUMBERS.fullmatch(text):
        return None
    out: set[int] = set()
    for tok in re.split(r"[\s,;]+", text):
        tok = tok.rstrip(".")
        if not tok:
            continue
        m = _RANGE.fullmatch(tok)
        if not m:
            return None
        a, b = int(m[1]), int(m[2] or m[1])
        a, b = min(a, b), max(a, b)
        if b - a > 1000:
            return None
        out.update(range(a, b + 1))
    return sorted(out) or None


class Agenda:
    def __init__(self, ai: AIAdapter, state_dir: Path, allowed: set[str], max_items: int, speakers: str, context: str,
                 include_previous: bool = True):
        self.ai, self.dir, self.allowed, self.max = ai, state_dir, allowed, max_items
        self.include_previous = include_previous
        self.speakers, self.context = speakers, context
        self.path = state_dir / "agenda.json"

    # ---- state ------------------------------------------------------------------------------
    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {"items": []}

    def _load(self) -> list[dict]:
        return self._read()["items"]

    def _write(self, state: dict) -> None:
        self.dir.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def _save(self, items: list[dict]) -> None:
        self._write({**self._read(), "items": items})

    def _set_pending(self, sender_id: str, what: str | None) -> None:
        """Per-sender 'the next message answers a command' state (kept in the state file)."""
        state = self._read()
        pending = state.get("pending", {})
        if what and pending.get(sender_id) != what:
            pending[sender_id] = what
        elif not what and sender_id in pending:
            del pending[sender_id]
        else:
            return
        self._write({**state, "pending": pending})

    def previous_questions(self) -> tuple[str, list[str]]:
        """(meeting stem, open questions) of the newest analysed meeting; ("", []) if disabled or none."""
        if not self.include_previous:
            return "", []
        files = sorted(self.dir.glob("*.analysis.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return "", []
        qs = [q for q in (json.loads(files[-1].read_text()).get("open_questions") or []) if isinstance(q, str) and q.strip()]
        return files[-1].name.removesuffix(".analysis.json"), qs

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
        items, (stem, qs) = self._load(), self.previous_questions()
        if not items and not qs:
            return "Список пуст. Пришлите темы для обсуждения."
        out = ["Пункты:\n" + "\n".join(f"{i}. {x['text']} ({x['from']})" for i, x in enumerate(items, 1))] if items \
            else ["Пунктов пока нет."]
        if qs:
            out.append(f"Открытые вопросы с прошлой встречи ({stem}), попадут в повестку:\n" + "\n".join(f"• {q}" for q in qs))
        return "\n\n".join(out)

    def delete(self, sender_id: str, arg: str) -> str:
        """/agenda_del 1 3 5-7 deletes several items at once; a bare /agenda_del waits for the numbers."""
        items = self._load()
        if not items:
            return "Список пуст."
        if not arg.strip():
            self._set_pending(sender_id, "del")
            return "Какие пункты удалить? Пришлите номера, например: 1 3 5-7. Посмотреть список — /agenda_list."
        nums = parse_numbers(arg)
        if nums is None:
            return "Не похоже на номера. Пример: /agenda_del 1 3 5-7"
        return self._delete_numbers(nums)

    def _delete_numbers(self, nums: list[int]) -> str:
        items = self._load()
        bad = [n for n in nums if not 1 <= n <= len(items)]
        if bad:                                              # all or nothing: never delete half of a request
            return f"Нет пунктов с номерами: {', '.join(map(str, bad))} (всего {len(items)}). Ничего не удалено."
        gone = [items[n - 1] for n in nums]
        keep = [x for i, x in enumerate(items, 1) if i not in set(nums)]
        self._save(keep)
        shown = "\n".join(f"{n}. {g['text'][:60]}" for n, g in zip(nums, gone))
        return f"Удалено ({len(gone)}):\n{shown}\nОсталось пунктов: {len(keep)}."

    def clear(self) -> str:
        n = len(self._load())
        self._save([])
        return f"Список очищен ({n})."

    def assemble(self) -> str:
        items, (stem, qs) = self._load(), self.previous_questions()
        if not items and not qs:
            return "Список пуст. Пришлите темы для обсуждения."
        listing = "\n".join(f"{i}. {x['text']}" + (f" ({x['from']})" if x.get("from") else "") for i, x in enumerate(items, 1)) \
            or "(nothing submitted)"
        previous = "\n".join(f"- {q}" for q in qs)
        try:
            text = self.ai.generate_text(prompts.agenda(listing, previous, self.speakers, self.context))
        except QuotaExceeded:
            return "Дневной лимит AI исчерпан. Список сохранён, повторите /agenda позже."
        except Exception as e:
            print(f"  agenda: assembling failed: {str(e)[:150]}", flush=True)
            return f"Не удалось собрать повестку ({str(e)[:100]}). Список сохранён, повторите /agenda."
        folder = self.dir / "agendas"
        folder.mkdir(parents=True, exist_ok=True)
        base, n = time.strftime("%Y%m%d-%H%M%S"), 1
        while (target := folder / (f"{base}.md" if n == 1 else f"{base}-{n}.md")).exists():
            n += 1                                     # several rebuilds in one second must not overwrite
        target.write_text(text + "\n")
        return text + "\n\nСписок сохранён: /clear — начать новый."

    # ---- Telegram-style entry point -------------------------------------------------------------
    def answer(self, msg: IncomingMessage) -> str:
        if msg.sender_id not in self.allowed:
            return "Нет доступа."
        text = msg.text.strip()
        if text.startswith("/"):
            self._set_pending(msg.sender_id, None)          # any command cancels a waiting question
            head, _, arg = text.partition(" ")
            name = head[1:].split("@")[0].lower()
            name = ALIASES.get(name, name)
            if name == "agenda":
                return self.assemble()
            if name == "agenda_add":
                return self.add(msg.sender_id, msg.sender_name, arg) if arg.strip() else \
                    "Пришлите пункты следующим сообщением (каждая строка — пункт). Команда не обязательна: любой обычный текст добавляется как пункты."
            if name == "agenda_list":
                return self.show()
            if name == "agenda_del":
                return self.delete(msg.sender_id, arg)
            if name == "agenda_clear":
                return self.clear()
            return HELP
        if self._read().get("pending", {}).get(msg.sender_id) == "del":
            nums = parse_numbers(text)
            if nums is None:
                return "Не похоже на номера пунктов. Пришлите номера, например: 1 3 5-7. Отмена — любая команда, например /agenda_list."
            self._set_pending(msg.sender_id, None)
            return self._delete_numbers(nums)
        if parse_numbers(text) is not None:                  # "1, 2, 3" is almost surely not an agenda item
            return ("Похоже на номера пунктов. Чтобы удалить: /agenda_del 1 2 3. "
                    "Если это действительно пункт — отправьте его так: /agenda_add <текст>.")
        return self.add(msg.sender_id, msg.sender_name, text)

    def handle(self, messenger: MessengerAdapter, messages: list[IncomingMessage]) -> None:
        for m in messages:
            try:
                messenger.reply(m, self.answer(m))
            except Exception as e:  # one bad message must not stop the others
                print(f"  agenda: failed to handle a message: {str(e)[:120]}", flush=True)
