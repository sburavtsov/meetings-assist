"""Messenger adapter: Telegram Bot API (stdlib only).

Config mapping: messenger.group = the group's chat id (e.g. -100123...), messenger.chat = the forum topic id
inside it ("" = General / a normal group), messenger.approver = the Telegram user id who may approve.
The bot must be a member (admin for forum topics) of the group; the approver must have pressed /start in the bot.
Secret: TELEGRAM_BOT_TOKEN in ~/.meetings-assist/secrets.json.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import secrets_config
from adapters.base import Decision, MessengerAdapter


class TelegramAdapter(MessengerAdapter):
    name = "telegram"

    def _call(self, method: str, params: dict | None = None):
        token = secrets_config.get("TELEGRAM_BOT_TOKEN")
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = urllib.parse.urlencode(params or {}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
                return json.load(r)["result"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Telegram {method} -> {e.code}: {e.read().decode()[:300]}") from e

    def _quiet(self, method: str, params: dict) -> None:
        try:
            self._call(method, params)
        except Exception as e:
            print(f"  telegram: {method} skipped ({str(e)[-60:]})")

    @property
    def _group(self) -> str:
        v = str(self.section["group"])
        if not v:
            raise RuntimeError("setting 'messenger.group' is not set (Telegram group chat id; "
                               "find it with: uv run meetings.py discover)")
        return v

    @property
    def _approver(self) -> str:
        v = str(self.section["approver"])
        if not v:
            raise RuntimeError("setting 'messenger.approver' is not set (your Telegram user id; "
                               "press /start in the bot, then: uv run meetings.py discover)")
        return v

    def check(self) -> str:
        return f"bot @{self._call('getMe')['username']}"

    def send(self, text: str) -> str:
        params = {"chat_id": self._group, "text": text, "disable_web_page_preview": "true"}
        if str(self.section["chat"]):
            params["message_thread_id"] = str(self.section["chat"])
        return str(self._call("sendMessage", params)["message_id"])

    def ask_approval(self, approval_id: str, preview: str) -> str:
        kb = {"inline_keyboard": [[
            {"text": "✅ Отправить", "callback_data": f"send:{approval_id}"},
            {"text": "✖ Не отправлять", "callback_data": f"skip:{approval_id}"}]]}
        res = self._call("sendMessage", {
            "chat_id": self._approver, "reply_markup": json.dumps(kb), "disable_web_page_preview": "true",
            "text": f"Предпросмотр — уйдёт в группу после подтверждения:\n\n{preview}"})
        return str(res["message_id"])

    def poll_decisions(self) -> list[Decision]:
        """Only callback queries are requested, so group chatter is never read."""
        off_file = self.state_dir / ".telegram_offset"
        params = {"timeout": 0, "allowed_updates": json.dumps(["callback_query"])}
        if off_file.exists():
            params["offset"] = off_file.read_text().strip()
        out = []
        for u in self._call("getUpdates", params):
            off_file.write_text(str(u["update_id"] + 1))
            cq = u.get("callback_query")
            if not cq:
                continue
            if str(cq["from"]["id"]) != self._approver:
                self._quiet("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "Нет доступа"})
                continue
            action, _, aid = cq.get("data", "").partition(":")
            if action in ("send", "skip"):
                out.append(Decision(aid, action, ref=cq))
        return out

    def acknowledge(self, decision: Decision, note: str) -> None:
        cq = decision.ref
        self._quiet("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note})
        self._quiet("editMessageReplyMarkup", {
            "chat_id": cq["message"]["chat"]["id"], "message_id": cq["message"]["message_id"],
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": note, "callback_data": "done"}]]})})

    def discover(self) -> list[tuple[str, str]]:
        """Private users who pressed /start (candidates for 'approver') and chats the bot sees ('group')."""
        found: dict[str, str] = {}
        for u in self._call("getUpdates", {"allowed_updates": json.dumps(["message", "my_chat_member"])}):
            m = u.get("message") or u.get("my_chat_member") or {}
            c = m.get("chat") or {}
            if c.get("type") == "private" and "from" in m:
                found[str(m["from"]["id"])] = f"user {m['from'].get('first_name', '')} -> messenger.approver"
            elif c:
                found[str(c["id"])] = f"{c.get('type')} {c.get('title', '')} -> messenger.group"
        return list(found.items())
