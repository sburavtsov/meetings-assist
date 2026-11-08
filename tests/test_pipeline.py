"""End to end through meetings.run_pipeline with fake adapters: plan -> analyze -> verify -> tasks -> preview -> tap."""
import json
import time

import pytest

import media
import meetings
import notify
import settings
from adapters.base import Decision
from conftest import FakeAI, msg


class Msgr:
    name, max_length = "fake", 4096
    section = {"group": "G", "chat": ""}

    def __init__(self):
        self.previews, self.sent = [], []

    def ask_approval(self, aid, text):
        self.previews.append(text); return "p"

    def send(self, text):
        self.sent.append(text); return "m"

    def acknowledge(self, d, note):
        pass


class Tasks:
    name = "fake"

    def __init__(self):
        self.pushed = []

    def push(self, stem, data):
        self.pushed.append(stem); return {"created": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    rec = tmp_path / "rec"; rec.mkdir()
    settings.S["watch"].update(dir=str(rec), min_words=1)
    settings.S["agenda"].update(enabled=True)
    monkeypatch.setenv("MEETINGS_ASSIST_STATE", str(tmp_path / "state.db"))
    monkeypatch.setattr(media, "speech_tracks", lambda *a, **k: None)
    monkeypatch.setattr(media, "extract_audio", lambda src, tracks, dst: dst.write_bytes(b"x"))
    svc = meetings.Services()
    svc._ai, svc._tasks = FakeAI(points=[{"topic": "Хэши", "question": "как", "from_items": [1]},
                                         {"topic": "Подписки", "question": "какую", "from_items": [2]},
                                         {"topic": "Бренд", "question": "название", "from_items": [3]}]), Tasks()
    m = Msgr()
    svc._notifier = notify.Notifier(m, rec / "transcripts", "P", tasks=lambda: svc.tasks,
                                    on_decision=lambda s, a: meetings.decide(s, a, svc))
    return svc, m, rec


def recording(rec, minutes_from_now):
    stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(time.time() + minutes_from_now * 60))
    p = rec / f"Aragast-{stamp}.mp4"; p.write_bytes(b"x")
    return p


def tap(svc, stem, action):
    svc.notifier.handle([Decision(notify.approval_id(stem), action, ref={})])


RESULT = {"summary": "s", "decisions": [], "tasks": [{"title": "Сделать хэши", "assignee": "Алиса"}],
          "open_questions": ["Успеем к четвергу?"], "follow_up_message": "Созвонились.", "speakers": [],
          "utterances": [{"speaker": "A", "start": 0, "text": "Таблицу хэшей делаем, договорились."}],
          "agenda_review": [{"n": 1, "status": "resolved", "evidence_quote": "Таблицу хэшей делаем, договорились"},
                            {"n": 2, "status": "discussed_open", "outcome": "отложили", "evidence_quote": "x"}]}


def test_full_cycle_two_meetings(env):
    svc, m, rec = env
    svc.agenda.allowed = {"1"}
    svc.agenda.answer(msg("1", "хэши\nподписки\nбренд", "Алиса"))
    svc.agenda.answer(msg("1", "/agenda"))
    svc.ai.result = RESULT
    r1 = recording(rec, 10)
    meetings.run_pipeline(r1, svc)
    out = rec / "transcripts"
    plan = svc.ai.plans[-1]
    assert [p["n"] for p in plan["agenda"]] == [1, 2, 3] and "tasks" not in plan
    saved = json.loads((out / f"{r1.stem}.analysis.json").read_text())
    assert [r["status"] for r in saved["agenda_review"]] == ["resolved", "discussed_open", "not_discussed"]
    assert svc.tasks.pushed == [r1.stem] and "Не успели обсудить:\n• Бренд — название" in m.previews[0]
    assert [x["text"] for x in svc.store.items()] == ["хэши", "подписки", "бренд"]          # waiting for the tap
    assert not (out / f"{r1.stem}.carryover.json").exists()
    tap(svc, r1.stem, "send")
    assert [x["text"] for x in svc.store.items()] == ["Подписки — какую", "Бренд — название", "Успеем к четвергу?"]
    assert json.loads((out / f"{r1.stem}.carryover.json").read_text())["added"] == 3
    meetings.run_pipeline(r1, svc)                                                       # re-run: nothing doubles
    assert len(svc.store.items()) == 3

    # meeting 2 without a new /agenda: the used agenda is never reviewed again; the earlier task is
    svc.ai.result = {**RESULT, "open_questions": [], "agenda_review": [], "task_followup": [{"n": 1, "status": "not_mentioned"}]}
    r2 = recording(rec, 20)
    meetings.run_pipeline(r2, svc)
    plan2 = svc.ai.plans[-1]
    assert "agenda" not in plan2 and plan2["tasks"] == [{"n": 1, "title": "Сделать хэши", "assignee": "Алиса"}]
    tap(svc, r2.stem, "send")
    assert "Статус по задаче: Сделать хэши (Алиса)" in [x["text"] for x in svc.store.items()]


def test_skipped_follow_up_leaves_list_and_agenda(env):
    svc, m, rec = env
    svc.agenda.answer(msg(str(settings.S["messenger"]["approver"]) or "x", "a"))      # (no access: approver unset)
    svc.agenda.allowed = {"1"}
    svc.agenda.answer(msg("1", "хэши\nподписки\nбренд")); svc.agenda.answer(msg("1", "/agenda"))
    svc.ai.result = RESULT
    r = recording(rec, 10)
    meetings.run_pipeline(r, svc)
    tap(svc, r.stem, "skip")
    assert [x["text"] for x in svc.store.items()] == ["хэши", "подписки", "бренд"]
    assert svc.store.agendas()[-1]["state"] == "open" and svc.store.carry(r.stem)["status"] == "discarded"


def test_no_message_step_applies_at_once(env):
    svc, m, rec = env
    svc.agenda.allowed = {"1"}
    svc.agenda.answer(msg("1", "хэши")); svc.agenda.answer(msg("1", "/agenda"))
    svc.ai.result = {**RESULT, "agenda_review": [{"n": 1, "status": "not_discussed"}]}
    meetings.run_pipeline(recording(rec, 10), svc, message=False)
    got = [x["text"] for x in svc.store.items()]
    assert got == ["Хэши — как", "Успеем к четвергу?"] and m.previews == [], got


def test_short_recording_frees_the_agenda(env):
    svc, m, rec = env
    settings.S["watch"]["min_words"] = 100
    svc.agenda.allowed = {"1"}
    svc.agenda.answer(msg("1", "хэши")); svc.agenda.answer(msg("1", "/agenda"))
    svc.ai.result = RESULT
    meetings.run_pipeline(recording(rec, 10), svc)
    assert svc.store.agendas()[-1]["state"] == "open" and svc.tasks.pushed == [] and m.previews == []


def test_agenda_disabled_changes_nothing(env):
    svc, m, rec = env
    settings.S["agenda"]["enabled"] = False
    svc.ai.result = {k: v for k, v in RESULT.items() if k != "agenda_review"}
    r = recording(rec, 10)
    meetings.run_pipeline(r, svc)
    assert svc.ai.plans == [None] and "plan" not in json.loads((rec / "transcripts" / f"{r.stem}.analysis.json").read_text())
    tap(svc, r.stem, "send")
    assert svc.store.items() == [] and not (rec / "transcripts" / f"{r.stem}.carryover.json").exists()
