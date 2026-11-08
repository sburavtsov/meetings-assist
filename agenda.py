"""Optional step: collect items to discuss from private messages to the bot, assemble the meeting agenda, and keep the
list in step with what the meetings actually covered.

Participants (the approver + agenda.contributors) send items to the bot in a private chat; every line is one item.
  /agenda        assemble the agenda with the AI and reply with it (as often as you like; the latest one counts)
  /agenda_add    add items (plain text works too)       /agenda_list   show the items
  /agenda_del 1 3 5-7  remove several items at once (bare /agenda_del: the next message is the numbers)
  /agenda_clear  start a new list                       /agenda_help
  (the old short names /list /del /clear /help /start still work). The table is COMMAND_NAMES below; it also
  drives the bot's command menu (`meetings.py bot-commands`). Texts: defaults/messages/<language>.json.

Life cycle (state in SQLite, see store.py):
  items --/agenda--> agenda (points remember the ids of the items they came from; a rebuild replaces the previous one)
  recording analysed  -> reserve(): the newest open agenda made before the recording is reserved for that meeting
                      -> prepare_carry(): what was not resolved waits for the follow-up decision
  follow-up sent      -> apply(): the agenda's items leave the list, the leftovers come back as carry-over items
  follow-up skipped   -> discard(): nothing changes in the list, the agenda is free again
Adding items never calls the AI: assembling is a separate call that returns structured points (JSON).
"""
from __future__ import annotations

import re

import plan as plan_mod
import prompts
from adapters.base import AIAdapter, IncomingMessage, MessengerAdapter, QuotaExceeded
from store import Store
from texts import t

COMMAND_NAMES = ["agenda", "agenda_add", "agenda_list", "agenda_del", "agenda_clear", "agenda_help"]
ALIASES = {"list": "agenda_list", "del": "agenda_del", "clear": "agenda_clear", "help": "agenda_help", "start": "agenda_help"}
AGENDA_SLACK = 30 * 60      # an agenda assembled up to 30 minutes after the recording started still belongs to it
ESCALATE_AFTER = 3          # a point carried this many times asks to be decided or dropped


def commands() -> list[tuple[str, str]]:
    """(name, description) for the bot's menu. Names: lowercase letters, digits, underscore (Telegram's rule)."""
    return [(n, t(f"cmd.{n}")) for n in COMMAND_NAMES]


def help_text() -> str:
    return t("help.intro") + "\n" + "\n".join(f"/{n} — {d}" for n, d in commands())


_BULLET = re.compile(r"^\s*(?:[-–—•*·]|\d+[.)])\s*")
_NUMBERS = re.compile(r"^[\d\s,;.\-–—]+$")
_RANGE = re.compile(r"(\d+)(?:[-–—](\d+))?")


def nickname(s: str) -> str:
    """'@Kostya' / 'kostya' -> '@kostya' (Telegram nicknames are case-insensitive); '' stays ''."""
    s = (s or "").strip().lstrip("@").lower()
    return "@" + s if s else ""


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


_S = lambda typ, **k: {"type": typ, **k}
AGENDA_SCHEMA = _S("object", properties={"points": _S("array", items=_S("object", properties={
    "topic": _S("string"), "question": _S("string"), "proposer": _S("string"),
    "from_items": _S("array", items=_S("integer")), "estimate_min": _S("integer")},
    required=["topic", "question", "from_items"]))}, required=["points"])


def render(points: list[dict]) -> str:
    """The agenda as a chat message."""
    lines = [t("agenda.title")]
    for p in points:
        tail = " (" + p["proposer"] + ")" if p.get("proposer") else ""
        c = p.get("carried", 0)
        mark = (t("agenda.mark_escalate", n=c + 1) if c >= ESCALATE_AFTER else t("agenda.mark_carried")) if c else ""
        lines.append(f"{p['n']}. {plan_mod.point_text(p)}{tail}{mark}")
    total = sum(int(p.get("estimate_min") or 0) for p in points)
    return "\n".join(lines) + ("\n\n" + t("agenda.estimate", min=total) if total else "")


def structure(raw: dict, items: list[dict]) -> list[dict]:
    """The AI's points -> clean numbered points that remember their item ids. Never lets an item vanish: items the AI
    forgot (or put under an empty topic) become points of their own; points not made of any listed item are dropped."""
    used, points = set(), []
    for p in raw.get("points") or []:
        if not (p.get("topic") or "").strip():
            continue                                   # its items stay unused, so they come back below
        src = list(dict.fromkeys(n for n in p.get("from_items") or []
                                 if isinstance(n, int) and 1 <= n <= len(items) and n not in used))
        if not src:
            continue                                   # invented by the AI, or only items already used: not an agenda point
        used.update(src)
        points.append({"topic": p["topic"].strip(), "question": (p.get("question") or "").strip(),
                       "proposer": (p.get("proposer") or "").strip(), "estimate_min": int(p.get("estimate_min") or 0),
                       "from_items": src, "carried": max([items[n - 1].get("carried", 0) for n in src] or [0])})
    for n, it in enumerate(items, 1):
        if n not in used:
            points.append({"topic": it["text"], "question": "", "proposer": "" if it.get("carried") else it.get("from", ""),
                           "estimate_min": 0, "from_items": [n], "carried": it.get("carried", 0)})
    for i, p in enumerate(points, 1):
        p["n"] = i
        p["item_ids"] = [items[n - 1]["id"] for n in p["from_items"] if "id" in items[n - 1]]
    return points


class Agenda:
    def __init__(self, ai: AIAdapter, store: Store, allowed: set[str], max_items: int, speakers: str, context: str):
        """`allowed`: user ids and/or "@nickname" entries (see may_use)."""
        self.ai, self.db, self.max = ai, store, max_items
        self.allowed = allowed
        self.speakers, self.context = speakers, context

    @property
    def allowed(self) -> set[str]:
        return self._ids | self._names

    @allowed.setter
    def allowed(self, entries) -> None:
        entries = {str(e).strip() for e in entries} - {""}
        self._names = {nickname(e) for e in entries if e.startswith("@")}
        self._ids = entries - {e for e in entries if e.startswith("@")}

    def may_use(self, msg: IncomingMessage) -> bool:
        """Ids are trusted as they are. A listed @nickname is pinned to the account that writes with it first; from then
        on only that account counts (it keeps access after renaming; someone who takes the old nickname does not)."""
        if msg.sender_id in self._ids:
            return True
        if any(self.db.pinned(n) == msg.sender_id for n in self._names):
            return True
        name = nickname(msg.sender_username)
        if name in self._names and self.db.pinned(name) is None and self.db.pin(name, msg.sender_id):
            print(f"  agenda: contributor {name} -> user id {msg.sender_id} (pinned on first message)", flush=True)
            return True
        return False

    # ---- chat operations (each returns the reply text) ---------------------------------------------
    def add(self, sender_id: str, name: str, text: str) -> str:
        new = parse_items(text)
        if not new:
            return help_text()
        if len(self.db.items()) >= self.max:
            return t("agenda.full", max=self.max)
        added, total = self.db.add_items([{"text": x, "from": name, "sender_id": sender_id} for x in new], limit=self.max)
        skipped = t("agenda.added_skipped", n=len(new) - added) if added < len(new) else ""
        return t("agenda.added", added=added, skipped=skipped, total=total)

    @staticmethod
    def _label(x: dict) -> str:
        if x.get("carried"):
            return f" ({t('agenda.label_carried')}{' ×' + str(x['carried']) if x['carried'] > 1 else ''})"
        return f" ({x['from']})" if x.get("from") else ""

    def show(self) -> str:
        items = self.db.items()
        if not items:
            return t("agenda.empty")
        return t("agenda.items") + "\n" + "\n".join(f"{i}. {x['text']}{self._label(x)}" for i, x in enumerate(items, 1))

    def delete(self, sender_id: str, arg: str) -> str:
        """/agenda_del 1 3 5-7 deletes several items at once; a bare /agenda_del waits for the numbers."""
        if not self.db.items():
            return t("agenda.empty_short")
        if not arg.strip():
            self.db.set_pending(sender_id, "del")
            return t("agenda.del_ask")
        nums = parse_numbers(arg)
        return t("agenda.del_bad_arg") if nums is None else self._delete_numbers(nums)

    def _delete_numbers(self, nums: list[int]) -> str:
        items = self.db.items()
        bad = [n for n in nums if not 1 <= n <= len(items)]
        if bad:                                              # all or nothing: never delete half of a request
            return t("agenda.del_missing", bad=", ".join(map(str, bad)), total=len(items))
        gone = [items[n - 1] for n in nums]
        self.db.delete_items([g["id"] for g in gone])
        shown = "\n".join(f"{n}. {g['text'][:60]}" for n, g in zip(nums, gone))
        return t("agenda.deleted", n=len(gone), shown=shown, left=len(items) - len(gone))

    def clear(self) -> str:
        return t("agenda.cleared", n=self.db.clear_items())

    def assemble(self) -> str:
        items = self.db.items()
        if not items:
            return t("agenda.empty")
        listing = "\n".join(f"{i}. {x['text']}" + (f" ({x['from']})" if x.get("from") else "") +
                            (f" [carry-over, already on {x['carried']} meeting(s)]" if x.get("carried") else "")
                            for i, x in enumerate(items, 1))
        try:
            raw = self.ai.generate_json(prompts.agenda(listing, self.speakers, self.context), AGENDA_SCHEMA)
        except QuotaExceeded:
            return t("agenda.quota")
        except Exception as e:
            print(f"  agenda: assembling failed: {str(e)[:150]}", flush=True)
            return t("agenda.failed", error=str(e)[:100])
        points = structure(raw, items)
        self.db.save_agenda(points)
        return render(points) + "\n\n" + t("agenda.saved")

    # ---- meeting life cycle (called by the pipeline) ---------------------------------------------------
    def reserve(self, stem: str, start: float, previous_start: float | None, max_age_days: float) -> dict | None:
        """Reserve the agenda of this meeting: the newest open one made before the recording (+ slack), after the
        previous meeting and not older than max_age_days. A meeting that is analysed again gets the same one back."""
        earliest = max(start - max_age_days * 86400, previous_start or 0)
        a = self.db.pick_agenda(stem, start + AGENDA_SLACK, earliest)
        if a and a["state"] == "open":
            self.db.set_agenda_state(a["id"], "reserved", stem)
        return a

    def prepare_carry(self, stem: str, points: list[dict], agenda_id: int | None) -> None:
        """Leftovers wait for the follow-up decision (apply / discard). Never overrides a finished decision."""
        c = self.db.carry(stem)
        if c and c["status"] != "pending":
            return
        self.db.set_carry(stem, "pending", points, agenda_id)

    def apply(self, stem: str, force: bool = False) -> int | None:
        """The meeting counts: its agenda's items leave the list, the leftovers come back. Returns how many were added
        (None if nothing was waiting)."""
        c = self.db.carry(stem)
        if not c or (c["status"] != "pending" and not force):
            return None
        if c["agenda_id"] and (a := self.db.agenda(c["agenda_id"])):
            self.db.delete_items([i for p in a["points"] for i in p.get("item_ids", [])])
            self.db.set_agenda_state(a["id"], "used", stem)
        have = {plan_mod.norm(x["text"]) for x in self.db.items()}
        new = [p for p in c["points"] if plan_mod.norm(p["text"]) not in have]
        self.db.add_items([{"text": p["text"], "carried": p["carried"], "from_meeting": stem, "why": p["why"]} for p in new])
        self.db.set_carry(stem, "applied", c["points"], c["agenda_id"])
        return len(new)

    def discard(self, stem: str) -> None:
        """The meeting does not count (follow-up not sent): the list stays, the agenda is free again."""
        c = self.db.carry(stem)
        if not c or c["status"] != "pending":
            return
        if c["agenda_id"] and (a := self.db.agenda(c["agenda_id"])) and a["state"] == "reserved":
            newer = any(x["id"] > a["id"] for x in self.db.agendas())
            self.db.set_agenda_state(a["id"], "replaced" if newer else "open", None)
        self.db.set_carry(stem, "discarded", c["points"], c["agenda_id"])

    # ---- chat entry point -----------------------------------------------------------------------------
    def answer(self, msg: IncomingMessage) -> str:
        if not self.may_use(msg):
            return t("agenda.no_access")
        text = msg.text.strip()
        if text.startswith("/"):
            self.db.set_pending(msg.sender_id, None)          # any command cancels a waiting question
            head, _, arg = text.partition(" ")
            name = head[1:].split("@")[0].lower()
            name = ALIASES.get(name, name)
            if name == "agenda":
                return self.assemble()
            if name == "agenda_add":
                return self.add(msg.sender_id, msg.sender_name, arg) if arg.strip() else t("agenda.add_how")
            if name == "agenda_list":
                return self.show()
            if name == "agenda_del":
                return self.delete(msg.sender_id, arg)
            if name == "agenda_clear":
                return self.clear()
            return help_text()
        if self.db.pending(msg.sender_id) == "del":
            nums = parse_numbers(text)
            if nums is None:
                return t("agenda.del_bad_pending")
            self.db.set_pending(msg.sender_id, None)
            return self._delete_numbers(nums)
        if parse_numbers(text) is not None:                  # "1, 2, 3" is almost surely not an agenda item
            return t("agenda.numbers_only")
        return self.add(msg.sender_id, msg.sender_name, text)

    def handle(self, messenger: MessengerAdapter, messages: list[IncomingMessage]) -> None:
        for m in messages:
            try:
                messenger.reply(m, self.answer(m))
            except Exception as e:  # one bad message must not stop the others
                print(f"  agenda: failed to handle a message: {str(e)[:120]}", flush=True)
