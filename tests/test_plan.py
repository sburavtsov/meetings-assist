import json
import os
import time

import plan as P
import prompts
from adapters.base import ANALYSIS_SCHEMA, analysis_schema, review_keys


def test_recording_time():
    t = P.recording_time("Aragast-2026-11-08 12-26-55", 5.0)
    assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) == "2026-11-08 12:26:55"
    assert P.recording_time("meeting", 123.0) == 123.0 and P.recording_time("x-2026-13-45 99-99-99", 7.0) == 7.0


def test_previous_meeting_by_recording_time_not_file_time(tmp_path):
    def meeting(stem, tasks, file_age, skipped=False):
        p = tmp_path / f"{stem}.analysis.json"
        p.write_text(json.dumps({"tasks": [{"title": x} for x in tasks]}))
        os.utime(p, (time.time() - file_age,) * 2)
        if skipped:
            (tmp_path / f"{stem}.skipped.json").write_text("{}")
    meeting("m-2026-11-01 10-00-00", ["old"], 10)                 # re-analysed recently (--force): newest FILE
    meeting("m-2026-11-05 10-00-00", ["real"], 9000)
    meeting("m-2026-11-06 10-00-00", [], 50, skipped=True)       # a short test recording in between
    meeting("m-2026-11-09 10-00-00", ["future"], 5)
    start = P.recording_time("m-2026-11-08 10-00-00", 0)
    stem, data, when = P.previous_meeting(tmp_path, "m-2026-11-08 10-00-00", start)
    assert stem == "m-2026-11-05 10-00-00" and data["tasks"][0]["title"] == "real" and when < start
    assert P.previous_meeting(tmp_path, "x", 0) is None


def test_build_plan():
    agenda = {"id": 7, "points": [{"n": 1, "topic": "Т", "question": "В", "carried": 2}]}
    prev = ("prev", {"tasks": [{"title": "T1", "assignee": "Боб"}, {"title": ""}], "decisions": [{"decision": "D1"}, "D2"]}, 0)
    pl = P.build_plan(agenda, prev)
    assert pl == {"agenda": [{"n": 1, "text": "Т — В", "carried": 2}], "agenda_id": 7,
                  "tasks": [{"n": 1, "title": "T1", "assignee": "Боб"}], "decisions": ["D1", "D2"], "previous": "prev"}
    assert P.build_plan(None, prev, tasks=False) is None and P.build_plan(None, None) is None


def test_schema_and_prompts_only_change_with_a_plan():
    plan_a = {"agenda": [{"n": 1, "text": "Тема — вопрос"}]}
    plan_t = {"tasks": [{"n": 1, "title": "Сделать", "assignee": "Боб"}], "decisions": ["Решили X"]}
    assert review_keys(None) == () and analysis_schema(None) == ANALYSIS_SCHEMA
    assert review_keys({**plan_a, **plan_t}) == ("agenda_review", "unplanned", "task_followup", "decisions_revisited")
    assert prompts.whole("A", "c", None) == prompts.transcribe("A", "c") + "\n" + prompts.analysis()
    w = prompts.whole("A", "c", {**plan_a, **plan_t})
    assert "1. Тема — вопрос" in w and "1. Сделать (Боб)" in w and "- Решили X" in w
    prompts.validate()


def test_verify_never_trusts_unproven_claims():
    tr = [{"text": "Давайте сделаем таблицу хэшей префиксов, решено."}]
    plan = {"agenda": [{"n": i, "text": f"p{i}"} for i in range(1, 5)], "tasks": [{"n": i, "title": f"t{i}"} for i in (1, 2, 3)]}
    data = {"utterances": tr,
            "agenda_review": [{"n": 1, "status": "resolved", "evidence_quote": "сделаем таблицу хэшей префиксов"},
                              {"n": 2, "status": "resolved", "evidence_quote": "никто такого не говорил"},
                              {"n": 3, "status": "weird"}],
            "task_followup": [{"n": 1, "status": "done", "evidence_quote": "таблицу хэшей... выдумка"},
                              {"n": 2, "status": "done", "evidence_quote": "выдумка выдумка выдумка"}]}
    P.verify(data, plan)
    assert [r["status"] for r in data["agenda_review"]] == ["resolved", "discussed_open", "not_discussed", "not_discussed"]
    assert data["agenda_review"][1]["downgraded"]
    assert [r["status"] for r in data["task_followup"]] == ["done", "not_mentioned", "not_mentioned"]


def test_carry_points():
    plan = {"agenda": [{"n": 1, "text": "A", "carried": 1}, {"n": 2, "text": "B"}, {"n": 3, "text": "C"}],
            "tasks": [{"n": 1, "title": "T1", "assignee": "Боб"}, {"n": 2, "title": "T2", "assignee": ""}]}
    data = {"agenda_review": [{"n": 1, "status": "partial"}, {"n": 2, "status": "resolved"}, {"n": 3, "status": "not_discussed"}],
            "task_followup": [{"n": 1, "status": "not_mentioned"}, {"n": 2, "status": "in_progress"}],
            "open_questions": ["A", "Новый?", "новый"]}
    got = [(c["text"], c["carried"]) for c in P.carry_points(data, plan)]
    assert got == [("A", 2), ("C", 1), ("Статус по задаче: T1 (Боб)", 1), ("Новый?", 1)]          # "новый" is the same question
    assert [c["text"] for c in P.carry_points(data, plan, include_open_questions=False)][-1] == "Статус по задаче: T1 (Боб)"
