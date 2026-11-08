"""Human-readable markdown of an analysis (adapter-independent)."""
from __future__ import annotations


def ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def render_markdown(stem: str, data: dict) -> str:
    ai = data.get("ai") or {}
    label = " ".join(x for x in (ai.get("adapter"), ai.get("model")) if x) or "AI"
    gaps = data.get("gaps") or []
    lines = [f"# {stem} ({label})", ""]
    if gaps:
        lines += ["> ⛔ The AI's content filter refused these stretches; they are NOT transcribed and "
                  "not analyzed: " + ", ".join(f"{ts(a)}–{ts(b)}" for a, b in gaps), ""]
    if data.get("summary"):
        lines += ["## Summary", "", data["summary"], ""]
    lines += ["## Decisions", ""]
    for d in data.get("decisions", []) or ["(none)"]:
        if isinstance(d, str):
            lines.append(f"- {d}")
            continue
        by = f" _({d['made_by']})_" if d.get("made_by") else ""
        lines.append(f"- {d['decision']}{by}")
        if d.get("evidence_quote"):
            lines.append(f"  > {d['evidence_quote']}")
    lines += ["", "## Tasks", ""]
    for t in data.get("tasks", []) or []:
        dl = f", deadline: {t['deadline']}" if t.get("deadline") else ""
        flag = " ⚠ low confidence" if t.get("confidence") == "low" else ""
        lines.append(f"- [ ] **{t['title']}** — {t['assignee']}{dl}{flag}")
        if t.get("description"):
            lines.append(f"  {t['description']}")
        if t.get("evidence_quote"):
            lines.append(f"  > {t['evidence_quote']}")
    if not data.get("tasks"):
        lines.append("(none)")
    if data.get("open_questions"):
        lines += ["", "## Open questions", ""] + [f"- {q}" for q in data["open_questions"]]
    if data.get("follow_up_message"):
        lines += ["", "## Follow-up message", "", data["follow_up_message"]]
    lines += ["", "## Transcript", ""]
    rows = []
    for u in data.get("utterances", []):
        flag = " ⚠" if u.get("confidence") == "low" else ""
        rows.append((u["start"], f"[{ts(u['start'])}] **{u['speaker']}:**{flag} {u['text']}"))
    for a, b in gaps:
        rows.append((a, f"[{ts(a)}] ⛔ _not transcribed (blocked by the AI content filter) until {ts(b)}_"))
    lines += [r[1] for r in sorted(rows, key=lambda r: r[0])]
    return "\n".join(lines) + "\n"
