"""ffmpeg / ffprobe helpers shared by the pipeline and the AI adapters (adapter-independent)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def audio_track_count(path: Path) -> int:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return len([l for l in r.stdout.splitlines() if l.strip()])


def mean_volume(path: Path, track: int) -> float:
    r = _run(["ffmpeg", "-nostats", "-i", str(path), "-map", f"0:a:{track}", "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else -91.0


def speech_tracks(path: Path, wanted: list[int], silence_db: float) -> list[int] | None:
    """The non-silent speech tracks among `wanted`, or None = use the default stream."""
    n = audio_track_count(path)
    if n == 0:
        raise RuntimeError("no audio stream")
    if n == 1:
        return None
    return [t for t in wanted if t < n and mean_volume(path, t) > silence_db] or None


def extract_audio(src: Path, tracks: list[int] | None, dest: Path) -> None:
    """Compact mono 16 kHz AAC; several tracks are mixed (e.g. mic + system audio)."""
    if tracks and len(tracks) > 1:
        ins = "".join(f"[0:a:{t}]" for t in tracks)
        sel = ["-filter_complex", f"{ins}amix=inputs={len(tracks)}:normalize=0[m]", "-map", "[m]"]
    elif tracks:
        sel = ["-map", f"0:a:{tracks[0]}"]
    else:
        sel = ["-vn"]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *sel,
                    "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(dest)], check=True)


def duration(path: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip() or 0)


def cut(audio: Path, start: float, length: float, dest: Path) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length),
                    "-i", str(audio), "-c:a", "aac", "-b:a", "48k", str(dest)], check=True)
