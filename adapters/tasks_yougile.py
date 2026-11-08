"""Task adapter: Yougile REST API v2 (stdlib only).

Config mapping: tasks.project / tasks.board / tasks.column are looked up BY TITLE; tasks.assignees maps a speaker
name to a Yougile account e-mail. tasks.create_missing=true lets the adapter create a missing board/column (the
project is never created). Card layout: defaults/cards/yougile.json, overridable in ~/.meetings-assist/cards/.
Secret: YOUGILE_API_KEY in ~/.meetings-assist/secrets.json (tools/yougile_setup_key.py creates it).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import plan as plan_mod
import secrets_config
import settings
from adapters.base import Card, TaskAdapter

_OPT = re.compile(r"\[\[(\w+):(.*?)\]\]", re.S)
_FIELD = re.compile(r"\{(\w+)\}")


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def render(tpl: str, data: dict, raw: tuple = (), escape: bool = True) -> str:
    """Fill a template: optional [[field: ...]] parts first, then {field} (HTML-escaped unless in `raw`)."""
    tpl = _OPT.sub(lambda m: m.group(2) if data.get(m.group(1)) else "", tpl)

    def fill(m):
        k = m.group(1)
        if k not in data:
            raise RuntimeError(f"cards/yougile.json: unknown field {{{k}}} (available: {sorted(data)})")
        v = str(data[k] if data[k] is not None else "")
        return v if (k in raw or not escape) else esc(v)
    return _FIELD.sub(fill, tpl)


def render_card(card: dict, data: dict) -> tuple[str, str]:
    out = []
    for blk in card["blocks"]:
        field = blk.get("when")
        if field and not data.get(field):
            continue
        ctx, raw = dict(data), ()
        if "item" in blk and field:
            items = []
            for el in data[field]:
                el_ctx = dict(el) if isinstance(el, dict) else {"value": el}
                for k in ("made_by", "evidence_quote", "decision"):
                    el_ctx.setdefault(k, "")
                items.append(render(blk["item"], el_ctx))
            ctx["items"], raw = "".join(items), ("items",)
        out.append(render(blk["html"], ctx, raw))
    return render(card["title"], data, escape=False), "".join(out)   # titles are plain text


def _idem(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:32]


class YougileAdapter(TaskAdapter):
    name = "yougile"

    def __init__(self, section: dict):
        super().__init__(section)
        self.cards = settings.layered_json("cards/yougile.json")

    # ---- HTTP -----------------------------------------------------------------------------
    def _api(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = self.cfg["api_base"] + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {secrets_config.get('YOUGILE_API_KEY')}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from e

    def _find(self, path: str, **query) -> list[dict]:
        return self._api("GET", path, query={"limit": 100, **query}).get("content", [])

    # ---- project / board / column ---------------------------------------------------------
    def _req(self, key: str) -> str:
        v = self.section.get(key)
        if not v:
            raise RuntimeError(f"setting 'tasks.{key}' is not set: add it to {settings.USER_CONFIG}")
        return str(v)

    def _resolve(self, create: bool) -> str | None:
        """Id of project/board/column from config. With create=False returns None for a missing column."""
        project_t, board_t, column_t = self._req("project"), self._req("board"), self._req("column")
        allow = bool(self.section.get("create_missing")) and create
        project = next((p for p in self._find("/projects", title=project_t) if p["title"] == project_t), None)
        if not project:
            raise RuntimeError(f"Yougile project {project_t!r} not found (tasks.project)")
        where = f"project {project_t!r}"
        board = next((b for b in self._find("/boards", projectId=project["id"], title=board_t)
                      if b["title"] == board_t), None)
        if board:
            board_id = board["id"]
        elif allow:
            board_id = self._api("POST", "/boards", {"title": board_t, "projectId": project["id"],
                                                     "idempotencyKey": _idem("board", project["id"], board_t)})["id"]
            print(f"created board {board_t!r} in {where}")
        elif create:
            raise RuntimeError(f"board {board_t!r} not found in {where} (tasks.board); "
                               f"set tasks.create_missing=true to let it be created")
        else:
            return None
        col = next((c for c in self._find("/columns", boardId=board_id, title=column_t)
                    if c["title"] == column_t), None)
        if col:
            return col["id"]
        if allow:
            col_id = self._api("POST", "/columns", {"title": column_t, "boardId": board_id,
                                                    "idempotencyKey": _idem("column", board_id, column_t)})["id"]
            print(f"created column {column_t!r}")
            return col_id
        if create:
            raise RuntimeError(f"column {column_t!r} not found in board {board_t!r} (tasks.column); "
                               f"set tasks.create_missing=true to let it be created")
        return None

    def check(self) -> str:
        col = self._resolve(create=False)
        t = f"{self._req('project')} / {self._req('board')} / {self._req('column')}"
        return f"{t}: " + ("found" if col else "board or column missing (create_missing=" +
                           str(bool(self.section.get('create_missing'))).lower() + ")")

    def _user_ids(self) -> dict[str, str]:
        out = {}
        for name, email in self.section["assignees"].items():
            hit = next((u for u in self._find("/users", email=email) if u.get("email", "").lower() == email.lower()), None)
            if hit:
                out[name] = hit["id"]
            else:
                print(f"  note: no Yougile user with e-mail {email} ({name}) - left unassigned")
        return out

    # ---- cards ----------------------------------------------------------------------------
    def preview(self, stem: str, analysis: dict) -> list[Card]:
        left = plan_mod.leftover(analysis)
        title, body = render_card(self.cards["meeting"], {
            "stem": stem, "summary": analysis.get("summary", ""), "decisions": analysis.get("decisions") or [],
            "open_questions": analysis.get("open_questions") or [], "gaps": analysis.get("gaps") or [],
            "not_discussed": left["not_discussed"], "discussed_open": left["open"]})
        out = [Card(title, body)]
        for t in analysis.get("tasks", []):
            who = t.get("assignee") or "unassigned"
            tt, tb = render_card(self.cards["task"], {
                "title": t["title"], "description": t.get("description", ""), "assignee": who,
                "deadline": t.get("deadline") or "", "confidence": t.get("confidence", ""),
                "low_confidence": "1" if t.get("confidence") == "low" else "",
                "evidence_quote": t.get("evidence_quote", "")})
            out.append(Card(tt, tb, who))
        return out

    def add_note(self, stem: str, title: str, text: str) -> str:
        """A card with a long plain text (the full follow-up when it does not fit into one messenger message)."""
        col = self._resolve(create=True)
        body = "".join(f"<p>{esc(par).replace(chr(10), '<br>')}</p>" for par in text.split("\n\n"))
        return self._api("POST", "/tasks", {"title": title, "columnId": col, "description": body,
                                            "idempotencyKey": _idem("note", stem, title)})["id"]

    def push(self, stem: str, analysis: dict) -> dict:
        cards = self.preview(stem, analysis)          # render first: a layout error fails before anything is created
        col = self._resolve(create=True)
        users = self._user_ids()
        ids = []
        for i, c in enumerate(cards):
            body = {"title": c.title, "columnId": col, "description": c.body,
                    "idempotencyKey": _idem("task", stem, str(i), c.title)}   # a retry never duplicates
            if c.assignee in users:
                body["assigned"] = [users[c.assignee]]
            ids.append(self._api("POST", "/tasks", body)["id"])
        print(f"pushed {len(ids)} cards ({len(cards) - 1} tasks + 1 meeting card)")
        return {"column": col, "created": ids}
