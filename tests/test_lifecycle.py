"""The agenda <-> meeting life cycle: the bugs this suite guards against (stale agendas, a list that never empties)."""
import time

import agenda as A
from conftest import FakeAI, msg

POINTS = [{"topic": "Хэши", "question": "как", "from_items": [1]}, {"topic": "Подписки", "question": "какую", "from_items": [2]}]


def setup(store):
    ag = A.Agenda(FakeAI(points=POINTS), store, {"1"}, 50, "", "")
    ag.answer(msg("1", "хэши\nподписки", "Алиса"))
    return ag


def test_rebuild_replaces_and_old_agenda_never_comes_back(store):
    ag = setup(store)
    ag.answer(msg("1", "/agenda")); ag.answer(msg("1", "/agenda"))                     # Monday: assembled twice
    a1, a2 = store.agendas()
    assert (a1["state"], a2["state"]) == ("replaced", "open")
    now = time.time()
    assert ag.reserve("monday", now + 60, None, 7)["id"] == a2["id"]
    ag.prepare_carry("monday", [], a2["id"]); ag.apply("monday")
    assert ag.reserve("wednesday", now + 86400, now + 60, 7) is None                  # the old rebuild is NOT picked


def test_agenda_belongs_to_one_meeting_only(store):
    ag = setup(store); ag.answer(msg("1", "/agenda"))
    now = time.time()
    a = ag.reserve("m1", now + 60, None, 7)
    assert ag.reserve("m2", now + 120, None, 7) is None and ag.reserve("m1", now + 60, None, 7)["id"] == a["id"]
    assert ag.reserve("late", now - 3 * 3600, None, 7) is None                       # agenda made after that recording
    assert ag.reserve("m3", now + 60, now + 30, 7) is None                            # previous meeting is newer than it


def test_send_consumes_items_and_carries_leftovers(store):
    ag = setup(store); ag.answer(msg("1", "/agenda"))
    ag.answer(msg("1", "добавлено после сборки"))
    a = ag.reserve("m1", time.time() + 60, None, 7)
    ag.prepare_carry("m1", [{"text": "Подписки — какую", "carried": 1, "why": "discussed_open"},
                            {"text": "Новый вопрос?", "carried": 1, "why": "open_question"}], a["id"])
    assert [x["text"] for x in store.items()] == ["хэши", "подписки", "добавлено после сборки"]   # nothing yet: waiting for the tap
    assert ag.apply("m1") == 2
    items = store.items()
    assert [x["text"] for x in items] == ["добавлено после сборки", "Подписки — какую", "Новый вопрос?"]
    assert items[1]["carried"] == 1 and items[1]["from_meeting"] == "m1"
    assert store.agenda(a["id"])["state"] == "used" and ag.apply("m1") is None                     # only once
    assert "2. Подписки — какую (перенос)" in ag.answer(msg("1", "/agenda_list"))


def test_skip_changes_nothing_and_frees_the_agenda(store):
    ag = setup(store); ag.answer(msg("1", "/agenda"))
    now = time.time()
    a = ag.reserve("test-recording", now + 60, None, 7)
    ag.prepare_carry("test-recording", [{"text": "мусор", "carried": 1, "why": "x"}], a["id"])
    ag.discard("test-recording")
    assert [x["text"] for x in store.items()] == ["хэши", "подписки"]
    assert store.agenda(a["id"])["state"] == "open" and ag.reserve("real", now + 120, None, 7)["id"] == a["id"]
    assert store.carry("test-recording")["status"] == "discarded" and ag.apply("test-recording") is None


def test_skip_after_a_rebuild_does_not_resurrect(store):
    ag = setup(store); ag.answer(msg("1", "/agenda"))
    a = ag.reserve("t", time.time() + 60, None, 7)
    ag.prepare_carry("t", [], a["id"])
    ag.answer(msg("1", "/agenda"))                                                     # rebuilt while waiting
    ag.discard("t")
    assert store.agenda(a["id"])["state"] == "replaced"


def test_carry_count_grows_and_escalates(store):
    ag = A.Agenda(FakeAI(points=[{"topic": "Вечная тема", "question": "", "from_items": [1]}]), store, {"1"}, 50, "", "")
    store.add_items([{"text": "Вечная тема", "carried": 0}])
    t0 = time.time()
    for i in range(1, 5):
        ag.answer(msg("1", "/agenda"))
        a = ag.reserve(f"m{i}", t0 + i * 100, None, 7)
        p = a["points"][0]
        ag.prepare_carry(f"m{i}", [{"text": "Вечная тема", "carried": p["carried"] + 1, "why": "not_discussed"}], a["id"])
        ag.apply(f"m{i}")
        assert [x["carried"] for x in store.items()] == [i]
    assert "переносится в 5-й раз" in ag.answer(msg("1", "/agenda"))
