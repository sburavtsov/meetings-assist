"""What was planned before a meeting, and what happened to it. Adapter-independent.

Before the meeting:  build_plan()   -> the agenda reserved for it (agenda.py) + the previous meeting's tasks/decisions,
                                       handed to AIAdapter.analyze(plan=...) so the AI checks them against the recording.
After the analysis:  verify()       -> checks every quote against the transcript; an unproven claim never hides a topic
                     carry_points() -> the points that go to the next meeting (code decides, the AI only classifies)
Everything here is deterministic and tested without an AI.

Statuses
  agenda_review[].status   resolved | discussed_open | partial | not_discussed   (carried unless resolved)
  task_followup[].status   done | in_progress | blocked | not_mentioned          (asked about again if blocked / not mentioned)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from texts import t as text_of

CARRY_STATUSES = {"discussed_open", "partial", "not_discussed"}
ASK_AGAIN_TASKS = {"blocked", "not_mentioned"}
_STEM_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T_](\d{2})-(\d{2})-(\d{2})")


def recording_time(stem: str, mtime: float) -> float:
    """Start of the recording: from a '2026-11-08 12-26-55' in its name, else the file's modification time."""
    m = _STEM_TIME.search(stem)
    if m:
        try:
            return datetime(*(int(x) for x in m.groups())).timestamp()      # rejects impossible dates (month 13...)
        except (ValueError, OverflowError, OSError):
            pass
    return mtime


def norm(s: str) -> str:
    """Text comparison key: lower case, punctuation and repeated spaces dropped."""
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


# ---- before the meeting -----------------------------------------------------------------------
def previous_meeting(out_dir: Path, stem: str, start: float) -> tuple[str, dict, float] | None:
    """(stem, analysis, recording time) of the latest meeting recorded before this one. Ordered by the recording time
    (name, else file time), not by when the analysis was written; recordings skipped as too short are not meetings."""
    best = None
    for p in out_dir.glob("*.analysis.json"):
        other = p.name.removesuffix(".analysis.json")
        if other == stem or (out_dir / f"{other}.skipped.json").exists():
            continue
        when = recording_time(other, p.stat().st_mtime)
        if when < start and (best is None or when > best[2]):
            try:
                best = (other, json.loads(p.read_text()), when)
            except (OSError, ValueError):
                continue                               # a broken file must never stop a meeting from being processed
    return best


def build_plan(agenda: dict | None, previous: tuple | None, tasks: bool = True) -> dict | None:
    """{"agenda": [...], "agenda_id": id, "tasks": [...], "decisions": [...], "previous": stem} or None.
    `agenda` is the reserved agenda (store row), `previous` the result of previous_meeting()."""
    plan: dict = {}
    if agenda and agenda.get("points"):
        plan["agenda"] = [{"n": p["n"], "text": point_text(p), **({"carried": p["carried"]} if p.get("carried") else {})}
                          for p in agenda["points"]]
        plan["agenda_id"] = agenda["id"]
    if tasks and previous:
        pt = [x for x in previous[1].get("tasks") or [] if x.get("title")]
        if pt:
            plan["tasks"] = [{"n": i, "title": x["title"], "assignee": x.get("assignee") or ""} for i, x in enumerate(pt, 1)]
            plan["decisions"] = [d if isinstance(d, str) else d.get("decision", "") for d in previous[1].get("decisions") or []]
            plan["previous"] = previous[0]
    return plan or None


def point_text(p: dict) -> str:
    """'topic — question' of an assembled agenda point."""
    return " — ".join(x for x in (p.get("topic", "").strip(), p.get("question", "").strip()) if x)


# ---- after the analysis -----------------------------------------------------------------------
def _quote_ok(quote: str | None, transcript: str) -> bool:
    if not quote:
        return False
    frags = [f for f in re.split(r"\.\.\.|…", quote) if len(norm(f)) >= 12]
    return bool(frags) and any(norm(f) in transcript for f in frags)


def verify(data: dict, plan: dict | None) -> dict:
    """Make the AI's review complete and honest (in place).
    - one entry per planned point / task, in order (missing ones are filled in as not_discussed / not_mentioned)
    - 'resolved' / 'done' / 'in_progress' need a quote found in the transcript; otherwise the topic is not trusted:
      resolved -> discussed_open, done / in_progress -> not_mentioned (carried / asked about again, never lost)"""
    plan = plan or {}
    transcript = norm(" ".join(u.get("text", "") for u in data.get("utterances") or []))
    if plan.get("agenda"):
        by_n = {r.get("n"): r for r in data.get("agenda_review") or [] if isinstance(r, dict)}
        review = []
        for p in plan["agenda"]:
            r = dict(by_n.get(p["n"]) or {"n": p["n"], "status": "not_discussed"})
            if r.get("status") not in CARRY_STATUSES | {"resolved"}:
                r["status"] = "not_discussed"
            r["quote_ok"] = _quote_ok(r.get("evidence_quote"), transcript)
            if r["status"] == "resolved" and not r["quote_ok"]:
                r["status"], r["downgraded"] = "discussed_open", True
            if r["status"] == "not_discussed":
                r["outcome"] = None
            review.append(r)
        data["agenda_review"] = review
    if plan.get("tasks"):
        by_n = {r.get("n"): r for r in data.get("task_followup") or [] if isinstance(r, dict)}
        follow = []
        for t in plan["tasks"]:
            r = dict(by_n.get(t["n"]) or {"n": t["n"], "status": "not_mentioned"})
            if r.get("status") not in ASK_AGAIN_TASKS | {"done", "in_progress"}:
                r["status"] = "not_mentioned"
            r["quote_ok"] = _quote_ok(r.get("evidence_quote"), transcript)
            if r["status"] in ("done", "in_progress") and not r["quote_ok"]:
                r["status"], r["downgraded"] = "not_mentioned", True
            if r["status"] == "not_mentioned":
                r["note"] = None
            follow.append(r)
        data["task_followup"] = follow
    return data


def carry_points(data: dict, plan: dict | None, include_open_questions: bool = True) -> list[dict]:
    """The agenda points for the next meeting: [{"text", "carried", "why"}]. `carried` = how many meetings the point has
    already been on (1 = this one). Deterministic: not 'resolved' carries, whatever the AI thought of it."""
    plan, out, seen = plan or {}, [], set()

    def add(text: str, carried: int, why: str) -> None:
        text, key = " ".join(text.split()), norm(text)
        if text and key not in seen:
            seen.add(key)
            out.append({"text": text, "carried": carried, "why": why})

    texts = {p["n"]: p for p in plan.get("agenda") or []}
    for r in data.get("agenda_review") or []:
        p = texts.get(r.get("n"))
        if p and r.get("status") in CARRY_STATUSES:
            add(p["text"], p.get("carried", 0) + 1, r["status"])
    for r in data.get("task_followup") or []:
        t = next((x for x in plan.get("tasks") or [] if x["n"] == r.get("n")), None)
        if t and r.get("status") in ASK_AGAIN_TASKS:
            who = f" ({t['assignee']})" if t.get("assignee") else ""
            add(text_of("agenda.task_status", title=t["title"], who=who), 1, "task_" + r["status"])
    if include_open_questions:
        for q in data.get("open_questions") or []:
            if isinstance(q, str):
                add(q, 1, "open_question")
    return out


def leftover(data: dict) -> dict[str, list[str]]:
    """The reviewed agenda of a meeting for display: {"not_discussed": [...], "open": [...], "resolved": [...]}."""
    texts = {p["n"]: p["text"] for p in (data.get("plan") or {}).get("agenda") or []}
    out: dict[str, list[str]] = {"not_discussed": [], "open": [], "resolved": []}
    for r in data.get("agenda_review") or []:
        if r.get("n") not in texts:
            continue
        key = {"resolved": "resolved", "discussed_open": "open"}.get(r.get("status"), "not_discussed")
        out[key].append(texts[r["n"]])
    return out
