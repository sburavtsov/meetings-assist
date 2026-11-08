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
from texts import t
from adapters.base import Decision, IncomingMessage, MessengerAdapter, Updates


class TelegramAdapter(MessengerAdapter):
    name = "telegram"
    max_length = 4096                                    # Telegram's limit for one message

    def _call(self, method: str, params: dict | None = None, timeout: int = 30):
        token = secrets_config.get("TELEGRAM_BOT_TOKEN")
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = urllib.parse.urlencode(params or {}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
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
            {"text": t("approval.send"), "callback_data": f"send:{approval_id}"},
            {"text": t("approval.skip"), "callback_data": f"skip:{approval_id}"}]]}
        res = self._call("sendMessage", {
            "chat_id": self._approver, "reply_markup": json.dumps(kb), "disable_web_page_preview": "true",
            "text": preview})
        return str(res["message_id"])

    def poll(self, want_messages: bool = False, wait: int = 0) -> Updates:
        """By default only button taps are requested, so group chatter is never read. With `want_messages` Telegram
        also delivers every message the bot can see (it is an admin of the group, so that includes the group's):
        everything that is not a private chat message is dropped right here, unread and unstored."""
        kinds = ["callback_query"] + (["message"] if want_messages else [])
        params = {"timeout": int(wait), "allowed_updates": json.dumps(kinds)}
        if off := self.state.get("telegram.offset"):
            params["offset"] = off
        decisions, messages = [], []
        updates = self._call("getUpdates", params, timeout=int(wait) + 20)
        if updates:                                      # one write per batch; Telegram drops what is before the offset
            self.state.set("telegram.offset", str(updates[-1]["update_id"] + 1))
        for u in updates:
            cq = u.get("callback_query")
            if cq:
                if str(cq["from"]["id"]) != self._approver:
                    self._quiet("answerCallbackQuery", {"callback_query_id": cq["id"], "text": t("approval.no_access")})
                    continue
                action, _, aid = cq.get("data", "").partition(":")
                if action in ("send", "skip"):
                    decisions.append(Decision(aid, action, ref=cq))
                continue
            m = u.get("message")
            if (want_messages and m and (m.get("chat") or {}).get("type") == "private"
                    and not (m.get("from") or {}).get("is_bot") and m.get("text")):
                messages.append(IncomingMessage(str(m["from"]["id"]), m["from"].get("first_name", ""),
                                                m["text"], str(m["chat"]["id"]), m["from"].get("username", "")))
        return Updates(decisions, messages)

    def reply(self, to: IncomingMessage, text: str) -> None:
        for i in range(0, len(text), self.max_length):    # a long reply goes as several messages
            self._call("sendMessage", {"chat_id": to.chat_ref, "text": text[i:i + self.max_length],
                                       "disable_web_page_preview": "true"})

    def acknowledge(self, decision: Decision, note: str) -> None:
        cq = decision.ref
        self._quiet("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note})
        self._quiet("editMessageReplyMarkup", {
            "chat_id": cq["message"]["chat"]["id"], "message_id": cq["message"]["message_id"],
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": note, "callback_data": "done"}]]})})

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """The menu behind the "/" button, for private chats only (the group's menu is left alone)."""
        self._call("setMyCommands", {
            "commands": json.dumps([{"command": n, "description": d} for n, d in commands], ensure_ascii=False),
            "scope": json.dumps({"type": "all_private_chats"})})

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
