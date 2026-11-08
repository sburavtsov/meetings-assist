import pytest

import agenda as A
from adapters.base import QuotaExceeded
from conftest import FakeAI, msg


def make(store, ai=None, allowed=("1", "2"), max_items=50):
    return A.Agenda(ai or FakeAI(), store, set(allowed), max_items, "Алиса, Боб", "студия")


def texts_of(store):
    return [x["text"] for x in store.items()]


def test_parse_items_and_numbers():
    assert A.parse_items("- one\n• two\n3) three\n\n  4. four  \n*five") == ["one", "two", "three", "four", "five"]
    assert A.parse_numbers("1, 2 3; 5-7") == [1, 2, 3, 5, 6, 7] and A.parse_numbers("7–5") == [5, 6, 7]
    for bad in ("", "1.5", "v2", "пункт 3", "1-9999999"):
        assert A.parse_numbers(bad) is None


def test_access_add_limit_list(store):
    ag = make(store, max_items=4)
    assert ag.answer(msg("9", "тема")) == "Нет доступа." and store.items() == []
    assert "Добавлено: 2" in ag.answer(msg("1", "- кнопка\n- бюджет", "Алиса"))
    assert "Добавлено: 2 (не поместилось: 1)" in ag.answer(msg("2", "a\nb\nc", "Боб"))
    assert "Список полон" in ag.answer(msg("1", "ещё"))
    lst = ag.answer(msg("1", "/agenda_list"))
    assert "1. кнопка (Алиса)" in lst and "3. a (Боб)" in lst and "4. b (Боб)" in lst
    assert ag.answer(msg("1", "/start")) == A.help_text() == ag.answer(msg("1", "/whatever"))


def test_delete_forms(store):
    ag = make(store)
    ag.answer(msg("1", "\n".join(f"пункт {i}" for i in range(1, 7))))
    assert "Нет пунктов с номерами: 9 (всего 6). Ничего не удалено." == ag.answer(msg("1", "/agenda_del 1 9"))
    r = ag.answer(msg("1", "/agenda_del 1 3 5-6"))
    assert "Удалено (4):" in r and "Осталось пунктов: 2." in r and texts_of(store) == ["пункт 2", "пункт 4"]
    assert "Какие пункты удалить?" in ag.answer(msg("1", "/del"))
    assert "Похоже на номера" in ag.answer(msg("2", "1"))                  # the question was asked to sender 1 only
    assert "Не похоже на номера пунктов" in ag.answer(msg("1", "первый"))
    assert "Удалено (1):" in ag.answer(msg("1", "1")) and texts_of(store) == ["пункт 4"]
    ag.answer(msg("1", "/agenda_del")); ag.answer(msg("1", "/agenda_list"))    # any command cancels the question
    assert "Похоже на номера" in ag.answer(msg("1", "1")) and len(store.items()) == 1
    assert "Добавлено: 1" in ag.answer(msg("1", "/agenda_add 2026")) and "Добавлено: 1" in ag.answer(msg("1", "1.5 часа"))
    assert "Пришлите пункты следующим сообщением" in ag.answer(msg("1", "/agenda_add"))
    assert "очищен (3)" in ag.answer(msg("1", "/agenda_clear"))


def test_assemble_structured_and_errors(store):
    ai = FakeAI(points=[{"topic": "Тема", "question": "решить", "proposer": "Алиса", "from_items": [1, 2], "estimate_min": 10}])
    ag = make(store, ai)
    assert "Список пуст" in ag.answer(msg("1", "/agenda")) and ai.prompts == []
    ag.answer(msg("1", "кнопка\nбюджет\nрелиз", "Алиса"))
    out = ag.answer(msg("1", "/agenda@bot"))
    assert out.startswith("Повестка встречи\n1. Тема — решить (Алиса)\n2. релиз (Алиса)") and "~10 мин" in out
    assert "1. кнопка (Алиса)" in ai.prompts[-1] and "Алиса, Боб" in ai.prompts[-1] and "студия" in ai.prompts[-1]
    (a,) = store.agendas()
    assert a["state"] == "open" and a["points"][0]["item_ids"] == [1, 2] and a["points"][1]["item_ids"] == [3]
    ag.ai = FakeAI(error=QuotaExceeded("q"))
    assert "лимит" in ag.answer(msg("1", "/agenda")).lower() and len(store.items()) == 3
    ag.ai = FakeAI(error=RuntimeError("503 overloaded"))
    assert "Не удалось" in ag.answer(msg("1", "/agenda")) and len(store.agendas()) == 1


def test_structure_never_loses_items():
    items = [{"id": 1, "text": "a", "from": "А"}, {"id": 2, "text": "b"}, {"id": 3, "text": "c", "carried": 1},
             {"id": 4, "text": "d", "carried": 3}]
    pts = A.structure({"points": [{"topic": "AB", "question": "q", "from_items": [1, 2, 99, 1], "estimate_min": 10},
                                  {"topic": " ", "from_items": [3]}, {"topic": "D", "question": "?", "from_items": [4]}]}, items)
    assert [p["topic"] for p in pts] == ["AB", "D", "c"] and pts[0]["item_ids"] == [1, 2]
    assert pts[1]["carried"] == 3 and pts[2]["item_ids"] == [3]
    txt = A.render(pts)
    assert "2. D — ? — переносится в 4-й раз: решить или убрать" in txt and "3. c (перенос)" in txt
    assert len(A.structure({}, items)) == 4
    invented = A.structure({"points": [{"topic": "выдумано", "from_items": []}, {"topic": "тоже", "from_items": [42]},
                                       {"topic": "a", "from_items": [1]}, {"topic": "снова a", "from_items": [1]}]}, items[:1])
    assert [p["topic"] for p in invented] == ["a"]


def test_handle_isolates_failures(store):
    class M:
        sent = []
        def reply(self, to, text):
            if to.sender_id == "2":
                raise RuntimeError("boom")
            self.sent.append(text)
    make(store).handle(M(), [msg("2", "x"), msg("1", "y")])
    assert len(M.sent) == 1


def test_menu_table_valid_for_telegram():
    import re
    names = [n for n, _ in A.commands()]
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", n) and 3 <= len(d) <= 256 for n, d in A.commands())
    assert all(v in names for v in A.ALIASES.values())
