"""Adapter interfaces. The pipeline talks only to these; each concrete system lives in its own module.

Three kinds, each picked by name in config.json (ai.adapter / messenger.adapter / tasks.adapter):
  AIAdapter         audio -> analysis dict (transcript, speakers, summary, decisions, tasks, ...)
  MessengerAdapter  post the follow-up to a group/chat, with a private preview + approve/skip step
  TaskAdapter       turn the analysis into cards/tasks in a project / board / column

To add a system: write adapters/<kind>_<name>.py with a subclass, register it in adapters/__init__.py,
add its defaults under "<kind>.adapters.<name>" in defaults/config.json, set "<kind>.adapter" to its name.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class AdapterError(RuntimeError):
    pass


class QuotaExceeded(AdapterError):
    """The AI's request quota is used up: retrying in seconds is pointless (the watcher waits)."""


# ----------------------------------------------------------------------------------------------
# The analysis contract: what every AIAdapter returns (and what is stored in <stem>.analysis.json).
# This is also the response schema handed to the AI, so field names here are fixed by the pipeline.
# ----------------------------------------------------------------------------------------------
ANALYSIS_SCHEMA = {'type': 'object',
 'properties': {'summary': {'type': 'string'},
                'decisions': {'type': 'array',
                              'items': {'type': 'object',
                                        'properties': {'decision': {'type': 'string'},
                                                       'made_by': {'type': 'string'},
                                                       'evidence_quote': {'type': 'string'}},
                                        'required': ['decision']}},
                'tasks': {'type': 'array',
                          'items': {'type': 'object',
                                    'properties': {'title': {'type': 'string'},
                                                   'description': {'type': 'string'},
                                                   'assignee': {'type': 'string'},
                                                   'deadline': {'type': 'string', 'nullable': True},
                                                   'evidence_quote': {'type': 'string'},
                                                   'confidence': {'type': 'string',
                                                                  'enum': ['high', 'medium', 'low']}},
                                    'required': ['title', 'assignee']}},
                'open_questions': {'type': 'array', 'items': {'type': 'string'}},
                'follow_up_message': {'type': 'string'},
                'speakers': {'type': 'array',
                             'items': {'type': 'object',
                                       'properties': {'label': {'type': 'string'},
                                                      'note': {'type': 'string'}},
                                       'required': ['label']}},
                'utterances': {'type': 'array',
                               'items': {'type': 'object',
                                         'properties': {'start': {'type': 'number'},
                                                        'speaker': {'type': 'string'},
                                                        'text': {'type': 'string'},
                                                        'confidence': {'type': 'string',
                                                                       'enum': ['high', 'medium', 'low']}},
                                         'required': ['start', 'speaker', 'text']}}},
 'required': ['utterances']}
TRANSCRIPT_KEYS = ("speakers", "utterances")
ANALYSIS_KEYS = ("summary", "decisions", "tasks", "open_questions", "follow_up_message")

# Added to the schema only when the meeting has a plan (see AIAdapter.analyze):
_S = lambda t, **k: {"type": t, **k}
REVIEW_PROPERTIES = {
    "agenda_review": _S("array", items=_S("object", properties={
        "n": _S("integer"),
        "status": _S("string", enum=["resolved", "discussed_open", "partial", "not_discussed"]),
        "outcome": _S("string", nullable=True), "evidence_quote": _S("string", nullable=True)}, required=["n", "status"])),
    "unplanned": _S("array", items=_S("string")),
    "task_followup": _S("array", items=_S("object", properties={
        "n": _S("integer"),
        "status": _S("string", enum=["done", "in_progress", "blocked", "not_mentioned"]),
        "note": _S("string", nullable=True), "evidence_quote": _S("string", nullable=True)}, required=["n", "status"])),
    "decisions_revisited": _S("array", items=_S("object", properties={
        "decision": _S("string"), "change": _S("string"), "evidence_quote": _S("string")}, required=["decision"])),
}


def review_keys(plan: dict | None) -> tuple:
    """Schema keys the plan adds: an agenda adds the agenda review, previous tasks add the task follow-up."""
    plan = plan or {}
    return (("agenda_review", "unplanned") if plan.get("agenda") else ()) + \
           (("task_followup", "decisions_revisited") if plan.get("tasks") else ())


def analysis_schema(plan: dict | None = None, only: tuple | None = None) -> dict:
    """ANALYSIS_SCHEMA (optionally restricted to `only` keys) plus the review keys the plan asks for."""
    props = {k: v for k, v in ANALYSIS_SCHEMA["properties"].items() if only is None or k in only}
    props.update({k: REVIEW_PROPERTIES[k] for k in review_keys(plan)})
    return {**ANALYSIS_SCHEMA, "properties": props}
# Besides the schema fields, an adapter adds:  "gaps": [[start_s, end_s], ...]  (stretches it could not process)
#                                              "ai": {"adapter": <name>, "model": <model used>}


class AIAdapter(ABC):
    name = ""

    def __init__(self, section: dict):
        self.section = section                       # the whole "ai" config section
        self.cfg = section["adapters"][self.name]    # this adapter's own settings

    @abstractmethod
    def analyze(self, audio: Path, speakers: str, context: str, plan: dict | None = None) -> dict:
        """`audio` is a compact mono audio file prepared by the pipeline. Returns the analysis dict.

        `plan` (optional) says what was planned before the meeting, so the AI can check what happened to it:
          {"agenda": [{"n": 1, "text": "..."}], "tasks": [{"n": 1, "title": "...", "assignee": "..."}],
           "decisions": ["..."]}
        An adapter must then add the keys named by review_keys(plan) to its answer, using analysis_schema(plan).
        """

    def generate_json(self, prompt: str, schema: dict) -> dict:
        """Structured answer to a text prompt (used by the optional agenda step)."""
        raise NotImplementedError(f"{self.name} adapter does not support generate_json")

    def check(self) -> str:
        """Verify credentials/connectivity (used by `meetings.py check --online`). Returns a short status."""
        return "no online check"


@dataclass
class Decision:
    """An approver's tap on a preview: `action` is "send" or "skip"."""
    approval_id: str
    action: str
    ref: object = None                               # adapter-private data needed by acknowledge()


@dataclass
class IncomingMessage:
    """A private message sent to the bot (never a group message: adapters drop those on arrival)."""
    sender_id: str
    sender_name: str
    text: str
    chat_ref: str                                    # adapter-private: where to reply
    sender_username: str = ""                        # the sender's public nickname without "@", if they have one


@dataclass
class Updates:
    decisions: list
    messages: list


class MessengerAdapter(ABC):
    name = ""
    max_length = 4000                                # longest text send() / ask_approval() accept (characters)

    def __init__(self, section: dict, state):
        self.section = section                       # "messenger" section: group, chat, approver, ...
        self.cfg = section["adapters"][self.name]
        self.state = state                           # small persistent key/value state: .get(key) / .set(key, value)

    @abstractmethod
    def send(self, text: str) -> str:
        """Post `text` to the configured group/chat. Returns the message id."""

    @abstractmethod
    def ask_approval(self, approval_id: str, preview: str) -> str:
        """Privately show `preview` to the approver with send / skip choices. Returns a reference id."""

    @abstractmethod
    def poll(self, want_messages: bool = False, wait: int = 0) -> Updates:
        """New events since the last call: approve/skip taps (only from the approver; others are refused inside)
        and, only if `want_messages`, private messages to the bot. Group chatter must never be returned or kept.
        `wait` > 0: block up to that many seconds until something arrives (long polling; used by the inbox thread)."""

    def reply(self, to: IncomingMessage, text: str) -> None:
        """Answer a private message."""
        raise NotImplementedError(f"{self.name} adapter does not support replies")

    def acknowledge(self, decision: Decision, note: str) -> None:
        """Best-effort cosmetic confirmation (toast, button text). Must never raise."""

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Publish [(name, description)] as the bot's command menu in private chats (optional capability)."""
        raise NotImplementedError(f"{self.name} adapter does not support a command menu")

    def discover(self) -> list[tuple[str, str]]:
        """Setup helper: [(id, description)] of the users/groups this account can see."""
        return []

    def check(self) -> str:
        return "no online check"

@dataclass
class Card:
    title: str
    body: str
    assignee: str = ""


class TaskAdapter(ABC):
    name = ""

    def __init__(self, section: dict):
        self.section = section                       # "tasks" section: project, board, column, assignees, ...
        self.cfg = section["adapters"][self.name]

    @abstractmethod
    def preview(self, stem: str, analysis: dict) -> list[Card]:
        """The cards that push() would create (no side effects)."""

    @abstractmethod
    def push(self, stem: str, analysis: dict) -> dict:
        """Create the cards in project/board/column. Must be safe to retry (no duplicates).
        Returns a JSON-able state dict, stored as <stem>.tasks.json."""

    def check(self) -> str:
        return "no online check"

    def add_note(self, stem: str, title: str, text: str) -> str:
        """Create one extra card holding a long plain text (e.g. a follow-up too long for the messenger). Must be safe
        to retry. Returns the card id. Optional capability."""
        raise NotImplementedError(f"{self.name} adapter does not support notes")
