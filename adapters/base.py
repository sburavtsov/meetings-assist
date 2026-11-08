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
# Besides the schema fields, an adapter adds:  "gaps": [[start_s, end_s], ...]  (stretches it could not process)
#                                              "ai": {"adapter": <name>, "model": <model used>}


class AIAdapter(ABC):
    name = ""

    def __init__(self, section: dict):
        self.section = section                       # the whole "ai" config section
        self.cfg = section["adapters"][self.name]    # this adapter's own settings

    @abstractmethod
    def analyze(self, audio: Path, speakers: str, context: str) -> dict:
        """`audio` is a compact mono audio file prepared by the pipeline. Returns the analysis dict."""

    def generate_text(self, prompt: str) -> str:
        """Plain text answer to a text prompt (used by the optional agenda step)."""
        raise NotImplementedError(f"{self.name} adapter does not support generate_text")

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


@dataclass
class Updates:
    decisions: list
    messages: list


class MessengerAdapter(ABC):
    name = ""

    def __init__(self, section: dict, state_dir: Path):
        self.section = section                       # "messenger" section: group, chat, approver, ...
        self.cfg = section["adapters"][self.name]
        self.state_dir = state_dir                   # where the adapter may keep its own small state

    @abstractmethod
    def send(self, text: str) -> str:
        """Post `text` to the configured group/chat. Returns the message id."""

    @abstractmethod
    def ask_approval(self, approval_id: str, preview: str) -> str:
        """Privately show `preview` to the approver with send / skip choices. Returns a reference id."""

    @abstractmethod
    def poll(self, want_messages: bool = False) -> Updates:
        """New events since the last call: approve/skip taps (only from the approver; others are refused inside)
        and, only if `want_messages`, private messages to the bot. Group chatter must never be returned or kept."""

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
