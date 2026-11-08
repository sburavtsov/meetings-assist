"""The Gemini adapter with the network stubbed out (needs the google-genai package; skipped without it)."""
import json

import pytest

G = pytest.importorskip("adapters.ai_google")
import media  # noqa: E402

RESULT = {"summary": "s", "decisions": [], "tasks": [], "open_questions": [], "follow_up_message": "", "utterances": [], "speakers": []}
PLAN = {"agenda": [{"n": 1, "text": "Тема — вопрос"}], "tasks": [{"n": 1, "title": "T", "assignee": "Б"}], "decisions": ["D"]}


@pytest.fixture
def ai(monkeypatch, tmp_path):
    calls = []

    def generate(client, models, contents, cfg):
        calls.append((contents, cfg.response_schema))
        if getattr(generate, "block_first", False) and len(calls) == 1:
            raise G.Blocked("filter")
        return type("R", (), {"text": json.dumps(RESULT)})(), "m"
    monkeypatch.setattr(G, "_upload", lambda c, a: "FILE")
    monkeypatch.setattr(G, "_delete", lambda c, f: None)
    monkeypatch.setattr(G, "_generate", generate)
    monkeypatch.setattr(media, "duration", lambda a: 60.0)
    a = G.GoogleAIAdapter({"adapter": "google", "adapters": {"google": {"models": ["m"], "chunk_seconds": 300, "min_piece_seconds": 60}}})
    a._client = lambda ms=None: None
    audio = tmp_path / "a.m4a"; audio.write_bytes(b"x")
    return a, audio, calls, generate


def test_plan_only_when_given(ai):
    a, audio, calls, _ = ai
    a.analyze(audio, "A", "ctx", None)
    assert "agenda_review" not in calls[-1][1]["properties"] and "PLANNED AGENDA" not in calls[-1][0][1]
    a.analyze(audio, "A", "ctx", PLAN)
    assert {"agenda_review", "task_followup"} <= set(calls[-1][1]["properties"]) and "1. T (Б)" in calls[-1][0][1]


def test_chunked_fallback_keeps_the_plan(ai):
    a, audio, calls, generate = ai
    generate.block_first = True
    a._chunked = lambda client, au, d, s, c: ([{"speaker": "A", "start": 1.0, "text": "слова"}], [{"label": "A"}], [])
    a.analyze(audio, "A", "ctx", PLAN)
    contents, schema = calls[-1]
    assert "agenda_review" in schema["properties"] and "utterances" not in schema["properties"]
    assert "1. Тема — вопрос" in contents[0] and "[00:01] A: слова" in contents[0]
