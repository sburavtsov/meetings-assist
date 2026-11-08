# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai"]
# ///
"""Meeting pipeline: recording -> AI analysis -> tasks in a task system -> follow-up in a messenger.

  uv run meetings.py process FILE [FILE...] [--force] [--no-push] [--no-message]   # manual
  uv run meetings.py watch [--no-push] [--no-message]                               # auto-tracking
  uv run meetings.py approvals          handle send / skip taps (and agenda messages) once
  uv run meetings.py agenda add "item" | list | del 1 3 5-7 | assemble | clear    optional agenda step, locally (see agenda.py)
  uv run meetings.py status             what is done / pending
  uv run meetings.py check [--online]   validate settings, prompts, card layout, adapters (--online: credentials)
  uv run meetings.py cards FILE         show the cards that would be created for an <stem>.analysis.json
  uv run meetings.py message FILE       show the follow-up message for an <stem>.analysis.json
  uv run meetings.py bot-commands [--show]   publish the agenda commands as the bot's menu (private chats only)
  uv run meetings.py discover           ids of users / groups the messenger account can see (for config.json)
  uv run meetings.py export-defaults    copy the default prompts + card layout to ~/.meetings-assist/ to edit

Adapters (config.json: ai.adapter / messenger.adapter / tasks.adapter) are in adapters/; settings in
defaults/config.json + ~/.meetings-assist/config.json; secrets in ~/.meetings-assist/secrets.json.

Each step leaves a marker in <watch dir>/transcripts/:
  <stem>.analysis.json / .analysis.md   transcript + analysis
  <stem>.tasks.json                     cards created in the task system
  <stem>.message.json                   follow-up state: pending / sent / skipped
  <stem>.skipped.json                   too little speech: tasks skipped on purpose
--force redoes the analysis (never re-creates cards: delete <stem>.tasks.json yourself for that).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import adapters
import agenda as agenda_mod
import media
import notify
import prompts
import report
import settings
from adapters.base import QuotaExceeded


class Services:
    """Adapters are created on first use, from the names in config.json."""

    def __init__(self):
        self._ai = self._tasks = self._notifier = self._agenda = None

    @property
    def ai(self):
        self._ai = self._ai or adapters.create_ai(settings.S["ai"])
        return self._ai

    @property
    def tasks(self):
        self._tasks = self._tasks or adapters.create_tasks(settings.S["tasks"])
        return self._tasks

    @property
    def notifier(self):
        if not self._notifier:
            sec = settings.S["messenger"]
            self._notifier = notify.Notifier(adapters.create_messenger(sec, settings.out_dir()),
                                             settings.out_dir(), sec["project_name"])
        return self._notifier

    @property
    def messenger(self):
        return self.notifier.m

    @property
    def agenda(self):
        if not self._agenda:
            a, m = settings.S["agenda"], settings.S["messenger"]
            allowed = {str(m["approver"])} | {str(x) for x in a["contributors"]}
            self._agenda = agenda_mod.Agenda(self.ai, settings.out_dir(), allowed - {""}, a["max_items"],
                                             settings.S["ai"]["speakers"], settings.S["ai"]["context"],
                                             include_previous=bool(a["include_open_questions"]))
        return self._agenda


W = settings.S["watch"]


def marker(path: Path, suffix: str) -> Path:
    return settings.out_dir() / f"{path.stem}{suffix}"


def is_stable(path: Path) -> bool:
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    return time.time() - st.st_mtime > W["settle_seconds"] and st.st_size > 0


def candidates() -> list[Path]:
    ext = {e.lower() for e in W["extensions"]}
    return sorted(p for p in settings.watch_dir().iterdir()
                  if p.is_file() and p.suffix.lower() in ext and not p.name.startswith("."))


def is_done(path: Path, push: bool) -> bool:
    if marker(path, ".skipped.json").exists():
        return True
    return marker(path, ".tasks.json" if push else ".analysis.json").exists()


def run_pipeline(path: Path, svc: Services, push: bool = True, force: bool = False, message: bool = True) -> None:
    out = settings.out_dir()
    out.mkdir(exist_ok=True)
    if force:
        for suf in (".analysis.json", ".analysis.md", ".skipped.json"):
            marker(path, suf).unlink(missing_ok=True)

    # 1. AI: transcript + speakers + decisions + tasks + follow-up
    aj = marker(path, ".analysis.json")
    if not aj.exists():
        tracks = media.speech_tracks(path, W["speech_tracks"], W["silence_db"])
        print(f"[1/3] analysis (tracks={tracks or 'default'})", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.m4a"
            media.extract_audio(path, tracks, audio)
            data = svc.ai.analyze(audio, settings.S["ai"]["speakers"], settings.S["ai"]["context"])
        aj.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        marker(path, ".analysis.md").write_text(report.render_markdown(path.stem, data))
        words = sum(len(u["text"].split()) for u in data.get("utterances", []))
        print(f"speakers: {[s['label'] for s in data.get('speakers', [])]}; {len(data.get('utterances', []))} utterances, "
              f"{len(data.get('gaps', []))} gaps -> {marker(path, '.analysis.md')}")
        if words < W["min_words"]:
            marker(path, ".skipped.json").write_text(json.dumps({"reason": f"only {words} words", "at": time.ctime()}))
            print(f"  only {words} words -> skipped (no tasks)")
            return
    else:
        print("[1/3] analysis exists", flush=True)
        data = json.loads(aj.read_text())

    # 2. task system
    if not push:
        print("[2/3] push disabled")
        return
    tj = marker(path, ".tasks.json")
    if tj.exists():
        print("[2/3] tasks already created", flush=True)
    else:
        print(f"[2/3] creating tasks ({svc.tasks.name})", flush=True)
        tj.write_text(json.dumps(svc.tasks.push(path.stem, data), indent=2))

    # 3. messenger preview (the approval itself happens later, by a tap)
    if message:
        print(f"[3/3] message preview ({svc.notifier.m.name})", flush=True)
        svc.notifier.request(path.stem, data)


_last_poll_error = ["", 0.0]


def poll_inbox(svc: Services) -> None:
    """One poll of the messenger: approve/skip taps -> Notifier, private messages -> Agenda (only if enabled)."""
    try:
        want = bool(settings.S["agenda"]["enabled"])
        up = svc.messenger.poll(want_messages=want)
        svc.notifier.handle(up.decisions)
        if want:
            svc.agenda.handle(svc.messenger, up.messages)
    except Exception as e:  # never let messenger trouble stop the pipeline
        msg = str(e)[:120]
        if msg != _last_poll_error[0] or time.time() - _last_poll_error[1] > 60:   # fast polling must not flood the log
            print(f"  inbox poll failed: {msg}", flush=True)
            _last_poll_error[:] = [msg, time.time()]


def flag(args: list[str], *names: str) -> bool:
    return any(n in args for n in names)


def cmd_process(args: list[str], svc: Services) -> None:
    files = [Path(a) for a in args if not a.startswith("--")]
    if not files:
        sys.exit(__doc__)
    for f in files:
        if not f.exists():
            sys.exit(f"no such file: {f}")
        print(f"== {f.name}", flush=True)
        run_pipeline(f, svc, push=not flag(args, "--no-push"), force=flag(args, "--force"),
                     message=not flag(args, "--no-message", "--no-telegram"))
        poll_inbox(svc)


def cmd_watch(args: list[str], svc: Services) -> None:
    push, message = not flag(args, "--no-push"), not flag(args, "--no-message", "--no-telegram")
    print(f"watching {settings.watch_dir()} (tasks: {push}, message: {message}); Ctrl+C to stop", flush=True)
    failed: dict[Path, float] = {}
    while True:
        for p in candidates():
            if is_done(p, push) or not is_stable(p):
                continue
            if time.time() - failed.get(p, 0) < W["retry_failed_after"]:
                continue
            print(f"== {p.name}", flush=True)
            try:
                run_pipeline(p, svc, push=push, message=message)
                failed.pop(p, None)
            except QuotaExceeded as e:
                failed[p] = time.time() + W["quota_retry_after"] - W["retry_failed_after"]
                print(f"  QUOTA: {e}\n  will retry in {W['quota_retry_after'] // 60} min", flush=True)
            except Exception:  # keep the watcher alive; retry later
                traceback.print_exc()
                failed[p] = time.time()
                print(f"  FAILED, will retry in {W['retry_failed_after'] // 60} min", flush=True)
        if message or settings.S["agenda"]["enabled"]:
            poll_inbox(svc)
        time.sleep(W["poll_seconds"])


def cmd_status() -> None:
    for p in candidates():
        done = [n for n, s in (("analysis", ".analysis.json"), ("tasks", ".tasks.json"), ("skipped", ".skipped.json"))
                if marker(p, s).exists()]
        m = marker(p, ".message.json")
        if m.exists():
            done.append("message:" + json.loads(m.read_text()).get("status", "sent"))
        print(f"{p.name:45s} {', '.join(done) or 'pending'}")


def _analysis(arg: str) -> tuple[str, dict]:
    p = Path(arg)
    return p.name.removesuffix(".analysis.json"), json.loads(p.read_text())


def cmd_cards(args: list[str], svc: Services) -> None:
    stem, data = _analysis(args[0])
    for c in svc.tasks.preview(stem, data):
        print(f"- {c.title}" + (f"  [{c.assignee}]" if c.assignee else "") + f"\n    {c.body}\n")


def cmd_message(args: list[str], svc: Services) -> None:
    stem, data = _analysis(args[0])
    msg = svc.notifier.message(stem, data)
    print(f"--- message ({len(msg)} chars) ---\n{msg}\n---")


def cmd_discover(svc: Services) -> None:
    rows = svc.notifier.m.discover()
    for ident, what in rows:
        print(f"{ident}\t{what}")
    if not rows:
        print("nothing yet: add the bot to the group / press Start in the bot, write a message, run again")


def cmd_check(args: list[str], svc: Services) -> None:
    ok = True

    def step(label: str, fn) -> None:
        nonlocal ok
        try:
            print(f"ok    {label}: {fn() or ''}")
        except Exception as e:
            ok = False
            print(f"FAIL  {label}: {str(e)[:200]}")
    online = "--online" in args
    step("settings", lambda: f"watching {settings.watch_dir()} -> {settings.out_dir()}")
    step("prompts", lambda: (prompts.validate(), "valid")[1])
    step(f"ai adapter [{settings.S['ai']['adapter']}]", lambda: (svc.ai, svc.ai.check() if online else "loaded")[1])
    step(f"tasks adapter [{settings.S['tasks']['adapter']}]",
         lambda: svc.tasks.check() if online else (svc.tasks._req("project"), "loaded")[1])
    sample = {"summary": "s", "decisions": [{"decision": "d", "made_by": "A", "evidence_quote": "q"}], "open_questions": ["?"],
              "tasks": [{"title": "t", "description": "d", "assignee": "A", "deadline": "today", "confidence": "low",
                         "evidence_quote": "q"}]}
    a = settings.S["agenda"]
    step("agenda step", lambda: ("enabled: contributors " + str(sorted({str(settings.S["messenger"]["approver"])} | {str(x) for x in a["contributors"]}))
                                 if a["enabled"] else "disabled (agenda.enabled=false)"))
    step("card layout", lambda: f"{len(svc.tasks.preview('check', sample))} sample cards render")

    def messenger():
        settings.require("messenger.group")
        settings.require("messenger.approver")
        return svc.notifier.m.check() if online else "loaded"
    step(f"messenger adapter [{settings.S['messenger']['adapter']}]", messenger)
    sys.exit(0 if ok else 1)


def cmd_agenda(args: list[str], svc: Services) -> None:
    """Local use of the agenda step (no messenger involved): add / list / assemble / clear."""
    sub = args[0] if args else ""
    if sub == "add" and len(args) > 1:
        print(svc.agenda.add("local", "local", "\n".join(args[1:])))
    elif sub == "list":
        print(svc.agenda.show())
    elif sub == "del" and len(args) > 1:
        print(svc.agenda.delete("local", " ".join(args[1:])))
    elif sub == "assemble":
        print(svc.agenda.assemble())
    elif sub == "clear":
        print(svc.agenda.clear())
    else:
        sys.exit('usage: meetings.py agenda add "item" ["item"...] | list | del 1 3 5-7 | assemble | clear')


def cmd_bot_commands(args: list[str], svc: Services) -> None:
    for name, desc in agenda_mod.COMMANDS:
        print(f"/{name:13s} {desc}")
    if "--show" in args:
        return
    svc.messenger.set_commands(agenda_mod.COMMANDS)
    print("published as the bot's menu in private chats")


def cmd_export_defaults() -> None:
    for path, fresh in settings.export_defaults():
        print(("written  " if fresh else "exists   ") + str(path))


def main() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"missing on PATH: {tool}")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args, svc = sys.argv[1], sys.argv[2:], Services()
    try:
        if cmd == "process":
            cmd_process(args, svc)
        elif cmd == "watch":
            cmd_watch(args, svc)
        elif cmd == "status":
            cmd_status()
        elif cmd == "approvals":
            poll_inbox(svc)
        elif cmd == "agenda":
            cmd_agenda(args, svc)
        elif cmd == "check":
            cmd_check(args, svc)
        elif cmd in ("cards", "message") and args:
            (cmd_cards if cmd == "cards" else cmd_message)(args, svc)
        elif cmd == "bot-commands":
            cmd_bot_commands(args, svc)
        elif cmd == "discover":
            cmd_discover(svc)
        elif cmd == "export-defaults":
            cmd_export_defaults()
        else:
            sys.exit(__doc__)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
