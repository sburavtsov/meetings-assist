"""AI prompts: defaults/prompts/*.txt, each optionally overridden by ~/.meetings-assist/prompts/<same name>.

  transcribe.txt     {speakers} {context}              how to transcribe + label speakers
  analysis.txt       (none)                            summary / decisions / tasks / follow-up rules
  analysis_only.txt  {speakers} {analysis} {transcript} used when the audio had to be chunked
  chunk_note.txt     {known}                           appended to every chunk's prompt
  agenda.txt         {items} {previous} {speakers} {context}  assembles the meeting agenda from submitted items
The JSON structure the AI must return is the contract in adapters/base.py, not part of the prompts.
"""
from __future__ import annotations

import string

import settings

FIELDS = {"transcribe.txt": {"speakers", "context"}, "analysis.txt": set(),
          "analysis_only.txt": {"speakers", "analysis", "transcript"}, "chunk_note.txt": {"known"},
          "agenda.txt": {"items", "speakers", "context", "previous"}}


def _load(name: str) -> str:
    text = settings.text(f"prompts/{name}")
    used = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    if used - FIELDS[name]:
        raise RuntimeError(f"prompts/{name}: unknown placeholder(s) {sorted(used - FIELDS[name])}; "
                           f"allowed: {sorted(FIELDS[name]) or 'none'}. Literal braces: {{{{ }}}}")
    return text


def analysis() -> str:
    return _load("analysis.txt")


def transcribe(speakers: str, context: str) -> str:
    return _load("transcribe.txt").format(speakers=speakers or "unknown", context=context or "none")


def whole(speakers: str, context: str) -> str:
    """Transcribe + analyse in one request."""
    return transcribe(speakers, context) + "\n" + analysis()


def chunk(speakers: str, context: str, known: list[str]) -> str:
    return transcribe(speakers, context) + _load("chunk_note.txt").format(known=", ".join(known) or "none yet")


def analysis_only(speakers: str, transcript: str) -> str:
    return _load("analysis_only.txt").format(speakers=speakers or "unknown", analysis=analysis(), transcript=transcript)


def agenda(items: str, previous: str, speakers: str, context: str) -> str:
    return _load("agenda.txt").format(items=items, previous=previous or "none", speakers=speakers or "unknown",
                                      context=context or "none")


def validate() -> None:
    for name in FIELDS:
        _load(name)
