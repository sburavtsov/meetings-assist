"""Follow-up message + approval workflow, written against the MessengerAdapter interface only.
(The pipeline polls the messenger once and hands the approve/skip taps to Notifier.handle().)

Flow: request()  -> private preview with send/skip to the approver, state <stem>.message.json = pending
      handle()   -> handles taps: send -> posts to the group/chat (state: sent), skip -> nothing is posted
A meeting is announced at most once: any existing state file blocks a second preview.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import plan as plan_mod
from adapters.base import MessengerAdapter


def header(stem: str, project_name: str) -> str:
    """'2026-11-03 · My Project' (date taken from the recording name if it has one)."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", stem)
    return " · ".join(x for x in (m.group(0) if m else "", project_name) if x)


def short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def leftover_block(data: dict) -> str:
    """'Не успели обсудить / Обсудили, но не решили' - what of the planned agenda goes to the next meeting."""
    left = plan_mod.leftover(data)
    parts = []
    for key, title in (("not_discussed", "Не успели обсудить:"), ("open", "Обсудили, но не решили:")):
        if left[key]:
            parts.append(title + "\n" + "\n".join(f"• {short(t, 100)}" for t in left[key]))
    return ("\n\n" + "\n\n".join(parts) + "\n\nЭти пункты перенесены в повестку следующей встречи.") if parts else ""


def build_message(data: dict, stem: str, project_name: str) -> str:
    """The AI's own short follow-up; if it is missing, a compact one built from decisions and tasks.
    A reviewed agenda adds what was left over (deterministic, not written by the AI)."""
    return _message(data, stem, project_name) + leftover_block(data)


def _message(data: dict, stem: str, project_name: str) -> str:
    head = header(stem, project_name)
    if data.get("follow_up_message"):
        return (head + "\n\n" if head else "") + data["follow_up_message"].strip()
    lines = [head, ""] if head else []
    if data.get("summary"):
        lines.append(short(data["summary"].split(". ")[0].rstrip("."), 160) + ".")
    if data.get("decisions"):
        lines += ["", "Решили:"] + [f"• {short(d['decision'])}" for d in data["decisions"][:4]]
    if data.get("tasks"):
        lines += ["", "Задачи:"] + [
            f"• {t['assignee']} — {short(t['title'], 60)}" + (f" ({t['deadline']})" if t.get("deadline") else "")
            for t in data["tasks"][:8]]
        if len(data["tasks"]) > 8:
            lines.append(f"…и ещё {len(data['tasks']) - 8} в трекере")
    return "\n".join(lines)


def approval_id(stem: str) -> str:
    return hashlib.sha1(stem.encode()).hexdigest()[:12]      # short enough for any button payload


class Notifier:
    def __init__(self, messenger: MessengerAdapter, out_dir: Path, project_name: str):
        self.m, self.out_dir, self.project = messenger, out_dir, project_name

    def _state(self, stem: str) -> Path:
        return self.out_dir / f"{stem}.message.json"

    def message(self, stem: str, data: dict) -> str:
        return build_message(data, stem, self.project)

    def request(self, stem: str, data: dict) -> None:
        state = self._state(stem)
        if state.exists():
            return                                           # already requested / sent / skipped
        aid = approval_id(stem)
        ref = self.m.ask_approval(aid, self.message(stem, data))
        state.write_text(json.dumps({"status": "pending", "approval_id": aid, "preview_ref": ref}))
        print("  message: preview sent to the approver, waiting for a tap")

    def handle(self, decisions: list) -> None:
        for d in decisions:
            found = None
            for f in self.out_dir.glob("*.message.json"):
                st = json.loads(f.read_text())
                if st.get("approval_id") == d.approval_id:
                    found = (f, st)
                    break
            if not found or found[1].get("status") != "pending":
                self.m.acknowledge(d, "Уже обработано")
                continue
            f, st = found
            stem = f.name.removesuffix(".message.json")
            if d.action == "send":
                data = json.loads((self.out_dir / f"{stem}.analysis.json").read_text())
                st.update(status="sent", message_id=self.m.send(self.message(stem, data)),
                          group=str(self.m.section["group"]), chat=str(self.m.section["chat"]))
                note = "✅ Отправлено"
            else:
                st["status"] = "skipped"
                note = "✖ Не отправлено"
            f.write_text(json.dumps(st))
            self.m.acknowledge(d, note)
            print(f"  message: {stem} -> {st['status']}")
