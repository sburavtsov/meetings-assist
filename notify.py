"""Follow-up message + approval workflow, written against the MessengerAdapter interface only.
(The inbox poller hands the approve/skip taps to Notifier.handle().)

Flow: request()  -> private preview with send/skip to the approver, state <stem>.message.json = pending
      handle()   -> handles taps: send -> posts to the group/chat (state: sent), skip -> nothing is posted;
                    then on_decision(stem, "send" | "skip") (the agenda step applies or discards its carry-over)
A meeting is announced at most once: any existing state file blocks a second preview.
A follow-up longer than the messenger allows is cut; its full text goes to one extra card in the task tracker.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import plan as plan_mod
from adapters.base import MessengerAdapter
from store import write_atomic
from texts import t


def header(stem: str, project_name: str) -> str:
    """'2026-11-03 · My Project' (date taken from the recording name if it has one)."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", stem)
    return " · ".join(x for x in (m.group(0) if m else "", project_name) if x)


def short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def leftover_block(data: dict) -> str:
    """What of the planned agenda goes to the next meeting (deterministic, not written by the AI)."""
    left = plan_mod.leftover(data)
    parts = []
    for key, title in (("not_discussed", t("msg.not_discussed")), ("open", t("msg.not_resolved"))):
        if left[key]:
            parts.append(title + "\n" + "\n".join(f"• {short(x, 100)}" for x in left[key]))
    return ("\n\n" + "\n\n".join(parts) + "\n\n" + t("msg.carried_note")) if parts else ""


def build_message(data: dict, stem: str, project_name: str) -> str:
    """The AI's own short follow-up; if it is missing, a compact one built from decisions and tasks.
    A reviewed agenda adds what was left over."""
    return _message(data, stem, project_name) + leftover_block(data)


def _message(data: dict, stem: str, project_name: str) -> str:
    head = header(stem, project_name)
    if data.get("follow_up_message"):
        return (head + "\n\n" if head else "") + data["follow_up_message"].strip()
    lines = [head, ""] if head else []
    if data.get("summary"):
        lines.append(short(data["summary"].split(". ")[0].rstrip("."), 160) + ".")
    if data.get("decisions"):
        lines += ["", t("msg.decided")] + [f"• {short(d['decision'])}" for d in data["decisions"][:4]]
    if data.get("tasks"):
        lines += ["", t("msg.tasks")] + [
            f"• {x['assignee']} — {short(x['title'], 60)}" + (f" ({x['deadline']})" if x.get("deadline") else "")
            for x in data["tasks"][:8]]
        if len(data["tasks"]) > 8:
            lines.append(t("msg.more_tasks", n=len(data["tasks"]) - 8))
    return "\n".join(lines)


def fit(text: str, limit: int, note: str) -> str:
    """`text` if it fits into `limit`, else its beginning (cut at a line or word break) + `note`."""
    if len(text) <= limit:
        return text
    room = limit - len(note)
    cut = text[:room]
    cut = cut[:cut.rfind("\n")] if cut.rfind("\n") > room * 0.6 else cut.rsplit(" ", 1)[0]
    return cut.rstrip() + note


def approval_id(stem: str) -> str:
    return hashlib.sha1(stem.encode()).hexdigest()[:12]      # short enough for any button payload


class Notifier:
    def __init__(self, messenger: MessengerAdapter, out_dir: Path, project_name: str,
                 tasks: Callable | None = None, on_decision: Callable[[str, str], None] | None = None):
        self.m, self.out_dir, self.project = messenger, out_dir, project_name
        self.tasks = tasks                       # () -> TaskAdapter, for the full text of an over-long follow-up
        self.on_decision = on_decision

    def _state(self, stem: str) -> Path:
        return self.out_dir / f"{stem}.message.json"

    def message(self, stem: str, data: dict) -> str:
        return build_message(data, stem, self.project)

    def note_title(self, stem: str) -> str:
        return t("msg.full_card_title", stem=stem)

    def outgoing(self, stem: str, data: dict, limit: int | None = None) -> str:
        """What is posted to the group: the message, cut to the messenger's limit if needed."""
        return fit(self.message(stem, data), limit or self.m.max_length, t("msg.too_long", title=self.note_title(stem)))

    def preview(self, stem: str, data: dict) -> str:
        """The approver sees what the group will get; the preview's own heading must not push it over the limit."""
        head = len(t("approval.preview", text=""))
        group = self.outgoing(stem, data)
        if head + len(group) > self.m.max_length:
            group = self.outgoing(stem, data, self.m.max_length - head)
        return t("approval.preview", text=group)

    def request(self, stem: str, data: dict) -> None:
        state = self._state(stem)
        if state.exists():
            return                                           # already requested / sent / skipped
        aid = approval_id(stem)
        ref = self.m.ask_approval(aid, self.preview(stem, data))
        write_atomic(state, json.dumps({"status": "pending", "approval_id": aid, "preview_ref": ref}))
        print("  message: preview sent to the approver, waiting for a tap")

    def handle(self, decisions: list) -> None:
        for d in decisions:
            found = None
            for f in self.out_dir.glob("*.message.json"):
                try:
                    st = json.loads(f.read_text())
                except ValueError:
                    continue
                if st.get("approval_id") == d.approval_id:
                    found = (f, st)
                    break
            if not found or found[1].get("status") != "pending":
                self.m.acknowledge(d, t("approval.done_already"))
                continue
            f, st = found
            stem = f.name.removesuffix(".message.json")
            if d.action == "send":
                data = json.loads((self.out_dir / f"{stem}.analysis.json").read_text())
                full, text = self.message(stem, data), self.outgoing(stem, data)
                if text != full:                             # too long: the full text goes to the tracker first
                    st["note_card"] = self.tasks().add_note(stem, self.note_title(stem), full)
                st.update(status="sent", message_id=self.m.send(text),
                          group=str(self.m.section["group"]), chat=str(self.m.section["chat"]))
                note = t("approval.sent")
            else:
                st["status"] = "skipped"
                note = t("approval.skipped")
            write_atomic(f, json.dumps(st))
            self.m.acknowledge(d, note)
            print(f"  message: {stem} -> {st['status']}")
            if self.on_decision:
                try:
                    self.on_decision(stem, d.action)
                except Exception as e:                       # the message is out; a carry-over problem must not undo that
                    print(f"  message: after-decision step failed for {stem}: {str(e)[:150]}", flush=True)
