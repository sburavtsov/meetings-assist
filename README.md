# meetings-assist

Turns a recorded work meeting into **tasks in your task tracker** and a **short follow-up in your team chat**.

```
recording (OBS / any video or audio)
  → AI: speaker-labelled transcript, summary, decisions, tasks, short follow-up
  → task system: one meeting card + one card per task (assigned by speaker name)
  → messenger: private preview to you with Send / Skip; posts to the group only after you approve
```

Ships with one adapter per kind — **Google Gemini**, **Telegram**, **Yougile** — behind small interfaces,
so other systems can be added without touching the pipeline.

## Requirements
macOS or Linux, [`uv`](https://docs.astral.sh/uv/), `ffmpeg` + `ffprobe`. (`brew install uv ffmpeg`)
The login-service installer is macOS (launchd) only; everything else is portable.

## Setup
```bash
mkdir -p ~/.meetings-assist && chmod 700 ~/.meetings-assist
cp examples/config.json  ~/.meetings-assist/config.json      # edit: folder, people, project/board/column, chat ids
cp examples/secrets.json ~/.meetings-assist/secrets.json     # edit: keys; then chmod 600 it
uv run meetings.py check --online                            # validates everything, tests the credentials
```
Secrets live **only** in `~/.meetings-assist/secrets.json` (an environment variable of the same name overrides).

| Secret | Where from |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio (enable billing: the free tier allows ~20 requests/day/model) |
| `TELEGRAM_BOT_TOKEN` | @BotFather. Add the bot to the group (admin for forum topics) and press **Start** in the bot |
| `YOUGILE_API_KEY` | `python3 tools/yougile_setup_key.py` (asks for your login/password, never stores them) |

Find the Telegram ids with `uv run meetings.py discover` (group → `messenger.group`, you → `messenger.approver`;
for a forum topic, `messenger.chat` is the topic id).

## Use
```bash
uv run meetings.py process "recording.mp4"   # one file, manually
uv run meetings.py watch                     # watch the folder (watch.dir)
python3 tools/install_service.py             # (macOS) run the watcher at login; --uninstall to remove
uv run meetings.py status                    # what is done / pending
uv run meetings.py approvals                 # handle Send / Skip taps once (the watcher does this itself)
uv run meetings.py cards FILE.analysis.json  # show the cards that would be created
uv run meetings.py message FILE.analysis.json
```
Each step leaves a marker next to the results (`<watch dir>/transcripts/`): `.analysis.json/.md`, `.tasks.json`,
`.message.json`, `.skipped.json`, so every step is idempotent and a crash resumes where it stopped.
Nothing is sent to the group without your tap.

## Optional: meeting agenda from the bot
Participants send **items to discuss** to the bot in a private chat. The items are kept in a list (a cache); the agenda is
assembled by the AI only when you ask, as a separate step, and you can rebuild it as often as you like.
```
(you → bot)  - cancel button: show the tutorial only to tap players     adds items, no AI call
             - sound budget
(you → bot)  /agenda   → "Повестка встречи  1. … — … (name)  2. … (перенос)  …  Ориентировочно: ~30 мин"
(you → bot)  - one more topic       then   /agenda   again → rebuilt with everything so far
```
Every line is one item. Commands (they share the `agenda_` prefix, so they form one group in the bot's menu):

| Command | Does | Old alias |
|---|---|---|
| `/agenda` | assemble / rebuild the agenda (the list is kept) | |
| `/agenda_add` | add items (plain text works too) | |
| `/agenda_list` | show the items and the carried-over questions | `/list` |
| `/agenda_del 1 3 5-7` | remove several items at once (bare `/agenda_del`: the next message is the numbers) | `/del` |
| `/agenda_clear` | start a new list | `/clear` |
| `/agenda_help` | how it works | `/help`, `/start` |

`uv run meetings.py bot-commands` publishes them as the bot's "/" menu (private chats only; `--show` just prints them).
The names are one table (`COMMANDS` in `agenda.py`), so renaming is a one-line change.
A message that consists only of numbers ("1, 2, 3") is never added as an item; the bot points to `/agenda_del` instead
(add such an item explicitly with `/agenda_add 2026`).
**After the meeting** the recording is checked against the plan (see below): whatever was not resolved comes back into the
list as a carry-over item, marked "(перенос)" (`×2`, `×3` … for repeats). `/agenda_list` shows them, `/agenda_del` removes them.

Enable it in `config.json`: `"agenda": {"enabled": true, "contributors": ["<telegram user id>"]}` (the approver is always
allowed; `uv run meetings.py discover` shows ids). Agendas are saved under `<watch dir>/transcripts/agendas/`.
The prompt is `prompts/agenda.txt` (overridable like the others). You can also use it without the bot:
`uv run meetings.py agenda add "item" | list | del 1 3 5-7 | assemble | clear | carry FILE.analysis.json`.

### Did we discuss what we planned? (agenda review and carry-over)
With the agenda step enabled, each recording is analysed **together with what was planned**:
- the newest assembled agenda made before the recording started (not older than `agenda.max_age_days`, claimed by one
  meeting only), and
- the tasks and decisions of the previous meeting (`agenda.review_tasks`).

The AI only **classifies** what happened to each point, with a verbatim quote: `resolved`, `discussed_open`, `partial`,
`not_discussed` (tasks: `done`, `in_progress`, `blocked`, `not_mentioned`). Plain code then decides, so the outcome is predictable:
- a quote that is not found in the transcript is not trusted: `resolved` becomes `discussed_open`, `done` / `in_progress`
  become `not_mentioned` - a topic is never lost because the AI was over-optimistic;
- everything except `resolved` goes to the next agenda, together with the meeting's open questions and the tasks that
  were `blocked` or `not_mentioned` ("Статус по задаче: …"); each carry increases a counter, and from the third time the agenda
  says "переносится в 3-й раз: решить или убрать";
- the follow-up message gets "Не успели обсудить" / "Обсудили, но не решили" (you see it in the private preview), the
  meeting card in the task tracker gets the same lists, and `<stem>.analysis.md` has the full review with quotes;
- topics that were discussed but not planned stay in the local analysis only.

Prompts: `prompts/agenda_review.txt`, `prompts/task_followup.txt`. Without an agenda and without earlier tasks nothing changes.
Redo the carry-over of an old analysis with `uv run meetings.py agenda carry FILE.analysis.json`.

**Privacy:** by default the bot only asks Telegram for button taps and never reads chat messages. With the agenda enabled it
must also receive messages; Telegram then delivers every message the bot can see, including the group's if the bot is an
admin there. The adapter drops everything that is not a private chat message immediately, unread and unstored.

## Configuration
`defaults/` (in the repo) holds every default; `~/.meetings-assist/` overrides it **per file**:

| File | Override |
|---|---|
| `config.json` | only the keys you change (deep merge; unknown keys are reported) |
| `prompts/*.txt` | replaces that one AI prompt (`uv run meetings.py export-defaults` copies the defaults to edit) |
| `cards/<tasks adapter>.json` | replaces the named card layouts (`meeting` / `task`) |

Main settings: `watch.*` (folder, file types, which audio tracks are speech, timings), `ai.speakers` / `ai.context`,
`tasks.project` / `board` / `column` / `assignees` / `create_missing`, `messenger.group` / `chat` / `approver` / `project_name`,
`agenda.enabled` / `contributors` / `max_items` / `include_open_questions` / `review` / `review_tasks` / `max_age_days`.
See [`examples/config.json`](examples/config.json) and [`defaults/config.json`](defaults/config.json).

The OBS multi-track layout is assumed by default (track 0 = mix, 1 = mic, 2 = system audio); change `watch.speech_tracks`
for other setups. Silent tracks are detected and skipped.

## Architecture
```
meetings.py        CLI + watcher          notify.py   follow-up message + approval workflow
agenda.py          optional agenda step      plan.py   what was planned vs what happened (review, carry-over)
media.py           ffmpeg helpers         prompts.py  prompt loading + placeholder checks
report.py          markdown report        settings.py / secrets_config.py   layered config / secrets
adapters/
  base.py              AIAdapter · MessengerAdapter · TaskAdapter + the analysis contract
  ai_google.py  messenger_telegram.py  tasks_yougile.py      (+ __init__.py: the registry)
defaults/          config.json, prompts/, cards/
tools/             yougile_setup_key.py, install_service.py
```
**Adding a system:** subclass the interface in `adapters/base.py` in `adapters/<kind>_<name>.py`, register it in
`adapters/__init__.py`, add its defaults under `<kind>.adapters.<name>` in `defaults/config.json`, and set
`ai.adapter` / `messenger.adapter` / `tasks.adapter` to its name.

## Notes
* Recordings and transcripts contain your meetings: they stay on your machine; only the audio goes to the AI provider you configure.
* Task and message texts are generated by an AI from the audio. Review the Telegram preview before tapping Send.

## License
MIT, see [LICENSE](LICENSE).
