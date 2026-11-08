"""AI adapter: Google Gemini (google-genai). Files API upload, structured JSON output, model fallback chain,
chunked fallback when the content filter refuses the whole file."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import types

import media
import prompts
import secrets_config
from adapters.base import ANALYSIS_KEYS, ANALYSIS_SCHEMA, TRANSCRIPT_KEYS, AIAdapter, QuotaExceeded


class Blocked(Exception):
    """The AI refused the input (content filter) instead of failing transiently."""


def _retry(fn, what: str, tries: int = 5):
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # 503 overload, transient SSL, etc.
            if "RESOURCE_EXHAUSTED" in str(e) and "PerDay" in str(e):
                raise QuotaExceeded(str(e)[:200]) from e
            if attempt == tries:
                raise
            wait = 15 * attempt
            print(f"{what}: attempt {attempt} failed ({str(e)[:80]}); retrying in {wait}s", flush=True)
            time.sleep(wait)


def _upload(client, path: Path):
    f = _retry(lambda: client.files.upload(file=str(path)), "upload")
    while f.state and f.state.name == "PROCESSING":
        time.sleep(3)
        f = client.files.get(name=f.name)
    if f.state and f.state.name == "FAILED":
        raise RuntimeError("upload processing failed")
    return f


def _generate(client, models: list[str], contents, cfg):
    """Return (resp, model). Raises Blocked on a content-filter refusal, RuntimeError if all models fail."""
    quota_hits = 0
    for m in models:
        print(f"analyzing with {m}...", flush=True)
        try:
            resp = _retry(lambda: client.models.generate_content(model=m, contents=contents, config=cfg),
                          f"generate[{m}]", tries=2)
        except QuotaExceeded:
            quota_hits += 1  # quota is per model: the next model may still have some
            print(f"{m}: daily quota used up", flush=True)
            continue
        except Exception as e:
            print(f"{m} gave up: {str(e)[:100]}", flush=True)
            continue
        if resp.text:
            return resp, m
        reason = resp.prompt_feedback.block_reason if resp.prompt_feedback else None
        raise Blocked(str(reason or "empty response"))
    if quota_hits == len(models):
        raise QuotaExceeded("daily Gemini quota used up on every model (free tier = 20 requests/day "
                            "per model). Enable billing on the Google project or retry tomorrow.")
    raise RuntimeError("all Gemini models failed")


def _delete(client, f) -> None:
    try:
        client.files.delete(name=f.name)
    except Exception:
        pass


class GoogleAIAdapter(AIAdapter):
    name = "google"

    def _client(self, timeout_ms: int | None = None):
        key = secrets_config.get("GEMINI_API_KEY")
        opts = types.HttpOptions(timeout=timeout_ms) if timeout_ms else None
        return genai.Client(api_key=key, http_options=opts)

    def check(self) -> str:
        client = self._client()
        names = {m.name.removeprefix("models/") for m in client.models.list()}
        missing = [m for m in self.cfg["models"] if m not in names]
        return f"{len(names)} models visible" + (f"; NOT available to this key: {missing}" if missing else "")

    def generate_text(self, prompt: str) -> str:
        client = self._client(120_000)
        try:
            resp, _ = _generate(client, self.cfg["models"], [prompt], types.GenerateContentConfig(temperature=0.3))
        except Blocked as e:
            raise RuntimeError(f"the AI refused to answer ({e})") from e
        return resp.text.strip()

    # ---- chunked fallback -------------------------------------------------------------------
    def _chunked(self, client, audio: Path, dur: float, speakers: str, context: str):
        """Transcribe in pieces when the whole file is refused. Returns (utterances, speakers, gaps)."""
        models, chunk_s, min_piece = self.cfg["models"], self.cfg["chunk_seconds"], self.cfg["min_piece_seconds"]
        schema = {"type": "object", "required": ["utterances"],
                  "properties": {k: ANALYSIS_SCHEMA["properties"][k] for k in TRANSCRIPT_KEYS}}
        cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.0)
        utterances, known, gaps = [], [], []

        def piece(start: float, length: float) -> None:
            with tempfile.TemporaryDirectory() as t2:
                part = Path(t2) / "p.m4a"
                media.cut(audio, start, length, part)
                f = _upload(client, part)
                try:
                    resp, _ = _generate(client, models, [f, prompts.chunk(speakers, context, known)], cfg)
                except Blocked as e:
                    if length / 2 >= min_piece:
                        print(f"  {start:.0f}s+{length:.0f}s blocked ({e}); splitting", flush=True)
                        piece(start, length / 2)
                        piece(start + length / 2, length / 2)
                    else:
                        print(f"  {start:.0f}s+{length:.0f}s blocked ({e}); marked as gap", flush=True)
                        gaps.append([start, min(start + length, dur)])
                    return
                finally:
                    _delete(client, f)
                for u in json.loads(resp.text).get("utterances", []):
                    u["start"] = float(u["start"]) + start
                    utterances.append(u)
                    if u["speaker"] not in known:
                        known.append(u["speaker"])

        pos = 0.0
        while pos < dur:
            print(f"chunk {pos / 60:.0f}-{min(pos + chunk_s, dur) / 60:.0f} min", flush=True)
            piece(pos, min(chunk_s, dur - pos))
            pos += chunk_s
        utterances.sort(key=lambda u: u["start"])
        return utterances, [{"label": k} for k in known], sorted(gaps)

    # ---- main entry -------------------------------------------------------------------------
    def analyze(self, audio: Path, speakers: str, context: str) -> dict:
        models = self.cfg["models"]
        dur = media.duration(audio)
        # Per-request timeout: generous for long audio, short enough to fall back from a hung model.
        client = self._client(int(max(120, 60 + dur * 0.5) * 1000))
        print(f"audio: {audio.stat().st_size / 1e6:.1f} MB; uploading...", flush=True)
        f = _upload(client, audio)
        t0 = time.time()
        cfg = types.GenerateContentConfig(response_mime_type="application/json",
                                          response_schema=ANALYSIS_SCHEMA, temperature=0.0)
        gaps: list = []
        try:
            resp, used = _generate(client, models, [f, prompts.whole(speakers, context)], cfg)
            data = json.loads(resp.text)
        except Blocked as e:
            print(f"whole file refused ({e}); falling back to chunks", flush=True)
            utts, spk, gaps = self._chunked(client, audio, dur, speakers, context)
            if not utts:
                raise RuntimeError("Gemini refused every part of the audio")
            text = "\n".join(
                [f"[{int(u['start']) // 60:02d}:{int(u['start']) % 60:02d}] {u['speaker']}: {u['text']}" for u in utts] +
                [f"[{int(a) // 60:02d}:{int(a) % 60:02d}] [NOT TRANSCRIBED] {int(a)}s-{int(b)}s" for a, b in gaps])
            text = "\n".join(sorted(text.split("\n")))
            a_cfg = types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.0,
                response_schema={"type": "object",
                                 "properties": {k: ANALYSIS_SCHEMA["properties"][k] for k in ANALYSIS_KEYS}})
            resp, used = _generate(client, models, [prompts.analysis_only(speakers, text)], a_cfg)
            data = json.loads(resp.text)
            data["utterances"], data["speakers"] = utts, spk
        finally:
            _delete(client, f)
        data["gaps"] = gaps
        data["ai"] = {"adapter": self.name, "model": used}
        print(f"done in {time.time() - t0:.0f}s", flush=True)
        return data
