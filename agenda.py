"""Optional step: collect items to discuss from private messages to the bot and assemble the meeting agenda.

Participants (the approver + agenda.contributors) send items to the bot in a private chat; every line is one item.
  /agenda        assemble the agenda with the AI and reply with it (as often as you like; the list is kept)
  /agenda_add    add items (plain text works too)       /agenda_list   show the items
  /agenda_del 1 3 5-7  remove several items at once (bare /agenda_del: the next message is the numbers)
  /agenda_clear  start a new list                       /agenda_help
  (the old short names /list /del /clear /help /start still work). The table is COMMANDS below; it also
  drives the bot's command menu (`meetings.py bot-commands`).
Adding items never calls the AI: the list is a cache, and assembling is a separate call. The AI returns the agenda as
structured points (JSON); the chat text is rendered from them.
After a meeting the pipeline checks the agenda against the recording (plan.py) and puts everything that was not resolved
- plus the meeting's open questions and tasks nobody mentioned - back into the list as carry-over items (add_carryover).
Items live in <out dir>/agenda.json; every assembled agenda is saved to <out dir>/agendas/<timestamp>.json (+ .md);
the .json is what the next recording is checked against.
Written against the AIAdapter / MessengerAdapter interfaces only; enabled with agenda.enabled in config.json.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import plan as plan_mod
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


_S = lambda t, **k: {"type": t, **k}
AGENDA_SCHEMA = _S("object", properties={"points": _S("array", items=_S("object", properties={
    "topic": _S("string"), "question": _S("string"), "proposer": _S("string"),
    "from_items": _S("array", items=_S("integer")), "estimate_min": _S("integer")},
    required=["topic", "question", "from_items"]))}, required=["points"])
ESCALATE_AFTER = 3                      # a point carried this many times asks to be dropped or decided


def render(points: list[dict]) -> str:
    """The agenda as a chat message."""
    lines = ["Повестка встречи"]
    for p in points:
        tail = " (" + p["proposer"] + ")" if p.get("proposer") else ""
        c = p.get("carried", 0)
        mark = (f" — переносится в {c + 1}-й раз: решить или убрать" if c >= ESCALATE_AFTER else " (перенос)") if c else ""
        lines.append(f"{p['n']}. {plan_mod.point_text(p)}{tail}{mark}")
    total = sum(int(p.get("estimate_min") or 0) for p in points)
    return "\n".join(lines) + (f"\n\nОриентировочно: ~{total} мин" if total else "")


def structure(raw: dict, items: list[dict]) -> list[dict]:
    """The AI's points -> clean numbered points. Never lets an item vanish: items the AI forgot become points of their own."""
    used, points = set(), []
    for p in raw.get("points") or []:
        if not (p.get("topic") or "").strip():
            continue                                   # its items stay unused, so they come back below as points of their own
        src = list(dict.fromkeys(n for n in p.get("from_items") or [] if isinstance(n, int) and 1 <= n <= len(items) and n not in used))
        used.update(src)
        points.append({"topic": p["topic"].strip(), "question": (p.get("question") or "").strip(),
                       "proposer": (p.get("proposer") or "").strip(), "estimate_min": int(p.get("estimate_min") or 0),
                       "from_items": src, "carried": max([items[n - 1].get("carried", 0) for n in src] or [0])})
    for n, it in enumerate(items, 1):
        if n not in used:
            points.append({"topic": it["text"], "question": "", "proposer": it.get("from", "") if not it.get("carried") else "",
                           "estimate_min": 0, "from_items": [n], "carried": it.get("carried", 0)})
    for i, p in enumerate(points, 1):
        p["n"] = i
    return points


class Agenda:
    def __init__(self, ai: AIAdapter, state_dir: Path, allowed: set[str], max_items: int, speakers: str, context: str,
                 include_previous: bool = True):
        self.ai, self.dir, self.allowed, self.max = ai, state_dir, allowed, max_items
        self.include_carryover = include_previous
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

    def add_carryover(self, stem: str, points: list[dict]) -> int:
        """Put the points a meeting left over into the list (after the people's own items). Returns how many were added."""
        items = self._load()
        have = {plan_mod._norm(x["text"]) for x in items}
        new = [p for p in points if plan_mod._norm(p["text"]) not in have]
        for p in new:
            items.append({"text": p["text"], "from": "", "sender_id": "", "at": time.strftime("%Y-%m-%d %H:%M"),
                          "carried": p["carried"], "from_meeting": stem, "why": p["why"]})
        if new:
            self._save(items)
        return len(new)

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

    @staticmethod
    def _label(x: dict) -> str:
        if x.get("carried"):
            return f" (перенос{' ×' + str(x['carried']) if x['carried'] > 1 else ''})"
        return f" ({x['from']})" if x.get("from") else ""

    def show(self) -> str:
        items = self._load()
        if not items:
            return "Список пуст. Пришлите темы для обсуждения."
        return "Пункты:\n" + "\n".join(f"{i}. {x['text']}{self._label(x)}" for i, x in enumerate(items, 1))

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
        items = self._load()
        if not items:
            return "Список пуст. Пришлите темы для обсуждения."
        listing = "\n".join(f"{i}. {x['text']}" + (f" ({x['from']})" if x.get("from") else "") +
                            (f" [carry-over, already on {x['carried']} meeting(s)]" if x.get("carried") else "")
                            for i, x in enumerate(items, 1))
        try:
            raw = self.ai.generate_json(prompts.agenda(listing, self.speakers, self.context), AGENDA_SCHEMA)
        except QuotaExceeded:
            return "Дневной лимит AI исчерпан. Список сохранён, повторите /agenda позже."
        except Exception as e:
            print(f"  agenda: assembling failed: {str(e)[:150]}", flush=True)
            return f"Не удалось собрать повестку ({str(e)[:100]}). Список сохранён, повторите /agenda."
        points = structure(raw, items)
        text = render(points)
        folder = self.dir / "agendas"
        folder.mkdir(parents=True, exist_ok=True)
        base, n = time.strftime("%Y%m%d-%H%M%S"), 1
        while (folder / (f"{base}.json" if n == 1 else f"{base}-{n}.json")).exists():
            n += 1                                     # several rebuilds in one second must not overwrite
        target = folder / (f"{base}.json" if n == 1 else f"{base}-{n}.json")
        target.write_text(json.dumps({"created": time.time(), "points": points}, ensure_ascii=False, indent=2))
        target.with_suffix(".md").write_text(text + "\n")
        return text + "\n\nСписок сохранён: /agenda_clear — начать новый."

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
