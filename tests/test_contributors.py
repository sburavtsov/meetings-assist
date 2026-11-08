"""Contributors listed by @nickname: pinned to the first account that writes with it."""
import agenda as A
from adapters.base import IncomingMessage
from conftest import FakeAI


def m(uid, username, text="тема"):
    return IncomingMessage(uid, "N", text, "c" + uid, username)


def make(store, entries):
    return A.Agenda(FakeAI(), store, set(entries), 50, "", "")


def test_nickname_normalisation():
    assert A.nickname("@Kostya") == A.nickname("kostya ") == "@kostya" and A.nickname("") == "" and A.nickname(None) == ""


def test_first_contact_pins_and_rename_keeps_access(store, capsys):
    ag = make(store, ["@Kostya", "111"])
    assert "Добавлено" in ag.answer(m("222", "kostya"))                     # case-insensitive, without "@"
    assert store.pinned("@kostya") == "222" and "@kostya -> user id 222" in capsys.readouterr().out
    assert "Добавлено" in ag.answer(m("222", "new_name"))                   # renamed: still in
    assert "Добавлено" in ag.answer(m("222", ""))                           # no nickname at all now: still in
    assert ag.answer(m("333", "kostya")) == "Нет доступа."                  # someone took the old nickname
    assert "Добавлено" in ag.answer(m("111", "")) and ag.answer(m("444", "")) == "Нет доступа."   # plain ids unchanged


def test_pin_survives_restart_and_removal_revokes(store):
    make(store, ["@kostya"]).answer(m("222", "kostya"))
    again = make(store, ["@kostya"])                                         # watcher restarted / settings reloaded
    assert "Добавлено" in again.answer(m("222", "whatever")) and again.answer(m("333", "kostya")) == "Нет доступа."
    without = make(store, ["111"])                                           # nickname removed from the config
    assert without.answer(m("222", "kostya")) == "Нет доступа."


def test_no_nickname_or_unlisted_nickname_is_refused(store):
    ag = make(store, ["@kostya"])
    assert ag.answer(m("222", "")) == "Нет доступа." and ag.answer(m("222", "stranger")) == "Нет доступа."
    assert store.pinned("@kostya") is None and store.pinned("@stranger") is None


def test_telegram_passes_the_nickname(store):
    from adapters.messenger_telegram import TelegramAdapter
    a = TelegramAdapter({"adapter": "telegram", "group": "-1", "chat": "", "approver": "7", "adapters": {"telegram": {}}}, store)
    a._call = lambda method, params=None, timeout=30: [
        {"update_id": 1, "message": {"chat": {"type": "private", "id": 9}, "from": {"id": 9, "first_name": "K", "username": "Kostya"},
                                     "text": "тема"}}] if method == "getUpdates" else {}
    (msg,) = a.poll(want_messages=True).messages
    assert msg.sender_username == "Kostya" and msg.sender_id == "9"
