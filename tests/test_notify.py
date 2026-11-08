import json

import notify
from adapters.base import Decision


class Msgr:
    name, max_length = "fake", 300
    section = {"group": "G", "chat": ""}

    def __init__(self):
        self.sent, self.previews, self.acks = [], [], []

    def ask_approval(self, aid, text):
        self.previews.append(text); return "p1"

    def send(self, text):
        self.sent.append(text); return "m1"

    def acknowledge(self, d, note):
        self.acks.append(note)


class Tasks:
    notes = []

    def add_note(self, stem, title, text):
        Tasks.notes.append((stem, title, text)); return "card-1"


def test_fit():
    assert notify.fit("short", 100, "!") == "short"
    long = "\n".join(f"строка номер {i} " * 3 for i in range(40))
    out = notify.fit(long, 200, " [дальше в трекере]")
    assert len(out) <= 200 and out.endswith(" [дальше в трекере]") and long.startswith(out[: -len(" [дальше в трекере]")])


def test_long_follow_up_goes_to_a_card(tmp_path):
    long = {"follow_up_message": "Итоги. " + "очень длинный текст " * 60}
    (tmp_path / "s.analysis.json").write_text(json.dumps(long))
    decided = []
    m = Msgr(); n = notify.Notifier(m, tmp_path, "P", tasks=lambda: Tasks(), on_decision=lambda s, a: decided.append((s, a)))
    n.request("s", long)
    assert len(m.previews[0]) <= m.max_length and "карточка «Итоги встречи (полный текст) s»" in m.previews[0]
    n.handle([Decision(notify.approval_id("s"), "send", ref={})])
    assert len(m.sent[0]) <= m.max_length and Tasks.notes[-1][2].endswith("очень длинный текст")
    st = json.loads((tmp_path / "s.message.json").read_text())
    assert st["status"] == "sent" and st["note_card"] == "card-1" and decided == [("s", "send")]
    n.handle([Decision(notify.approval_id("s"), "send", ref={})])
    assert m.acks[-1] == "Уже обработано" and len(m.sent) == 1 and decided == [("s", "send")]


def test_short_follow_up_no_card_and_skip(tmp_path):
    data = {"follow_up_message": "Коротко."}
    (tmp_path / "s.analysis.json").write_text(json.dumps(data))
    decided, Tasks.notes[:] = [], []
    m = Msgr(); n = notify.Notifier(m, tmp_path, "P", tasks=lambda: Tasks(), on_decision=lambda s, a: decided.append(a))
    n.request("s", data)
    assert m.previews == ["Предпросмотр — уйдёт в группу после подтверждения:\n\nP\n\nКоротко."]
    n.handle([Decision(notify.approval_id("s"), "skip", ref={})])
    assert m.sent == [] and Tasks.notes == [] and decided == ["skip"]


def test_leftover_block_in_message():
    data = {"follow_up_message": "Hi", "plan": {"agenda": [{"n": 1, "text": "A"}, {"n": 2, "text": "B"}, {"n": 3, "text": "C"}]},
            "agenda_review": [{"n": 1, "status": "resolved"}, {"n": 2, "status": "not_discussed"}, {"n": 3, "status": "discussed_open"}]}
    out = notify.build_message(data, "x-2026-01-02", "P")
    assert out.startswith("2026-01-02 · P\n\nHi") and "Не успели обсудить:\n• B" in out and "Обсудили, но не решили:\n• C" in out
    assert notify.build_message({"follow_up_message": "Hi"}, "x", "") == "Hi"
