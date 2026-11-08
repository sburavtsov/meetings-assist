"""What was planned before a meeting, and what happened to it. Adapter-independent.

Before the meeting:  build_plan()   -> the agenda that was assembled for it + the previous meeting's tasks/decisions,
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
import time
from datetime import datetime
from pathlib import Path

CARRY_STATUSES = {"discussed_open", "partial", "not_discussed"}
ASK_AGAIN_TASKS = {"blocked", "not_mentioned"}
AGENDA_SLACK = 30 * 60            # an agenda assembled up to 30 minutes after the recording started still belongs to it
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


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def _jsons(folder: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            out.append((p, json.loads(p.read_text())))
        except (OSError, ValueError):
            continue                                   # a broken file must never stop a meeting from being processed
    return out


# ---- before the meeting -----------------------------------------------------------------------
def pick_agenda(out_dir: Path, stem: str, rec_start: float, max_age_days: float = 7) -> tuple[Path, dict] | None:
    """The newest assembled agenda made before the recording started, not yet used by another meeting."""
    best = None
    for p, a in _jsons(out_dir / "agendas"):
        if a.get("used_by") not in (None, stem) or not a.get("points"):
            continue
        made = a.get("created", 0)
        if made > rec_start + AGENDA_SLACK or rec_start - made > max_age_days * 86400:
            continue
        if best is None or made >= best[1].get("created", 0):
            best = (p, a)
    return best


def previous_meeting(out_dir: Path, stem: str) -> tuple[str, dict] | None:
    """(stem, analysis) of the newest analysed meeting other than this one."""
    files = sorted((p for p in out_dir.glob("*.analysis.json") if p.name != f"{stem}.analysis.json"),
                   key=lambda p: p.stat().st_mtime)
    for p in reversed(files):
        try:
            return p.name.removesuffix(".analysis.json"), json.loads(p.read_text())
        except (OSError, ValueError):
            continue
    return None


def build_plan(out_dir: Path, stem: str, rec_start: float, agenda: bool = True, tasks: bool = True,
               max_age_days: float = 7) -> dict | None:
    """{"agenda": [...], "tasks": [...], "decisions": [...], "agenda_file": name, "previous": stem} or None."""
    plan: dict = {}
    if agenda and (a := pick_agenda(out_dir, stem, rec_start, max_age_days)):
        plan["agenda"] = [{"n": p["n"], "text": point_text(p), **({"carried": p["carried"]} if p.get("carried") else {})}
                          for p in a[1]["points"]]
        plan["agenda_file"] = a[0].name
    if tasks and (prev := previous_meeting(out_dir, stem)):
        pt = [t for t in prev[1].get("tasks") or [] if t.get("title")]
        if pt:
            plan["tasks"] = [{"n": i, "title": t["title"], "assignee": t.get("assignee") or ""} for i, t in enumerate(pt, 1)]
            plan["decisions"] = [d if isinstance(d, str) else d.get("decision", "") for d in prev[1].get("decisions") or []]
            plan["previous"] = prev[0]
    return plan or None


def point_text(p: dict) -> str:
    """'topic — question' of an assembled agenda point."""
    return " — ".join(x for x in (p.get("topic", "").strip(), p.get("question", "").strip()) if x)


def claim_agenda(out_dir: Path, plan: dict | None, stem: str) -> None:
    """Mark the agenda as used by this meeting so the next meeting cannot pick it again."""
    if not plan or not plan.get("agenda_file"):
        return
    p = out_dir / "agendas" / plan["agenda_file"]
    try:
        a = json.loads(p.read_text())
        if a.get("used_by") != stem:
            a["used_by"] = stem
            p.write_text(json.dumps(a, ensure_ascii=False, indent=2))
    except (OSError, ValueError):
        pass


# ---- after the analysis -----------------------------------------------------------------------
def _quote_ok(quote: str | None, transcript: str) -> bool:
    if not quote:
        return False
    frags = [f for f in re.split(r"\.\.\.|…", quote) if len(_norm(f)) >= 12]
    return bool(frags) and any(_norm(f) in transcript for f in frags)


def verify(data: dict, plan: dict | None) -> dict:
    """Make the AI's review complete and honest (in place).
    - one entry per planned point / task, in order (missing ones are filled in as not_discussed / not_mentioned)
    - 'resolved' / 'done' / 'in_progress' need a quote found in the transcript; otherwise the topic is not trusted:
      resolved -> discussed_open, done / in_progress -> not_mentioned (carried / asked about again, never lost)"""
    plan = plan or {}
    transcript = _norm(" ".join(u.get("text", "") for u in data.get("utterances") or []))
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
        text, key = " ".join(text.split()), _norm(text)
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
            add(f"Статус по задаче: {t['title']}{who}", 1, "task_" + r["status"])
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
