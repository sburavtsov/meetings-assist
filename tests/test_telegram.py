import json

from adapters.messenger_telegram import TelegramAdapter


def adapter(store, updates):
    a = TelegramAdapter({"adapter": "telegram", "group": "-100", "chat": "", "approver": "7", "adapters": {"telegram": {}}}, store)
    a.calls = []

    def call(method, params=None, timeout=30):
        a.calls.append((method, params, timeout))
        return updates.pop(0) if method == "getUpdates" else {"message_id": 1}
    a._call = call
    return a


def test_poll_filters_and_offset(store):
    ups = [[{"update_id": 10, "callback_query": {"id": "q", "from": {"id": 7}, "data": "send:abc", "message": {}}},
            {"update_id": 11, "callback_query": {"id": "q2", "from": {"id": 8}, "data": "send:abc"}},
            {"update_id": 12, "message": {"chat": {"type": "group", "id": -100}, "from": {"id": 7}, "text": "group chatter"}},
            {"update_id": 13, "message": {"chat": {"type": "private", "id": 7}, "from": {"id": 7, "first_name": "S"}, "text": "тема"}},
            {"update_id": 14, "message": {"chat": {"type": "private", "id": 9}, "from": {"id": 9, "is_bot": True}, "text": "x"}}],
           []]
    a = adapter(store, ups)
    u = a.poll(want_messages=True, wait=25)
    assert [d.approval_id for d in u.decisions] == ["abc"] and [m.text for m in u.messages] == ["тема"]
    method, params, timeout = a.calls[0]
    assert params["timeout"] == 25 and timeout > 25 and json.loads(params["allowed_updates"]) == ["callback_query", "message"]
    assert store.get("telegram.offset") == "15"
    assert any(c[0] == "answerCallbackQuery" and c[1]["text"] == "Нет доступа" for c in a.calls)
    a.poll()
    assert a.calls[-1][1]["offset"] == "15" and json.loads(a.calls[-1][1]["allowed_updates"]) == ["callback_query"]


def test_long_reply_is_split(store):
    a = adapter(store, [])
    from adapters.base import IncomingMessage
    a.reply(IncomingMessage("7", "S", "x", "7"), "я" * 9000)
    sent = [c[1]["text"] for c in a.calls if c[0] == "sendMessage"]
    assert [len(s) for s in sent] == [4096, 4096, 808]
