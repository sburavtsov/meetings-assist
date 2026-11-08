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
`.message.json`, `.carryover.json`, `.skipped.json`, so every step is idempotent and a crash resumes where it stopped
(all files are written atomically). Nothing is sent to the group without your tap.

How the watcher behaves:
- **A recording is processed only when it is finished**: not written for `watch.settle_seconds` *and* not open in any
  process (`lsof`), so a paused OBS recording is never picked up half-way (`watch.skip_open_files`).
- **The bot answers at once**, also while a recording is being analysed: taps and agenda messages are handled by a separate
  thread with long polling.
- **Only one copy runs**: `watch`, `process` and `approvals` take a lock; a second one exits with the PID of the first.
- **Saved settings apply without a restart**: changes to `~/.meetings-assist/config.json`, `prompts/`, `cards/` or
  `messages/` are picked up within one `watch.poll_seconds`; a file with a syntax error keeps the previous settings and
  says so in the log.
- **A long follow-up** (over Telegram's 4096 characters) is cut at a line break; its full text goes to an extra card
  in the task tracker, and the message says which one.
- **Local state** (agenda list, agendas, the bot's update offset) is a SQLite database, `~/.meetings-assist/state.db`,
  deliberately outside any synced folder. Older `agenda.json` / `agendas/` / `.telegram_offset` files are imported once.

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
| `/agenda` | assemble / rebuild the agenda (the latest one counts) | |
| `/agenda_add` | add items (plain text works too) | |
| `/agenda_list` | show the items, carry-overs marked "(перенос)" | `/list` |
| `/agenda_del 1 3 5-7` | remove several items at once (bare `/agenda_del`: the next message is the numbers) | `/del` |
| `/agenda_clear` | start a new list | `/clear` |
| `/agenda_help` | how it works | `/help`, `/start` |

`uv run meetings.py bot-commands` publishes them as the bot's "/" menu (private chats only; `--show` just prints them).
The names are one list (`COMMAND_NAMES` in `agenda.py`), the descriptions are in `messages/ru.json`.
A message that consists only of numbers ("1, 2, 3") is never added as an item; the bot points to `/agenda_del` instead
(add such an item explicitly with `/agenda_add 2026`).
**The list follows the meetings.** When a recording is analysed, the latest agenda assembled before it is reserved for
that meeting (a rebuild replaces the earlier version; an agenda is used by one meeting only). When you tap **Send** on the
follow-up, the items that agenda was made of leave the list and whatever was not resolved comes back as carry-over items,
marked "(перенос)" (`×2`, `×3` … for repeats); items sent after the agenda was assembled stay. When you tap **Don't send**
(e.g. a test recording), the list is left as it was and the agenda is free for the next recording.

Enable it in `config.json`: `"agenda": {"enabled": true, "contributors": ["@kostya", "123456789"]}`. The approver is always
allowed. Each person presses **Start** in the bot once (a bot cannot write first).
- **`@nickname`**: the first account that writes to the bot with that nickname is pinned to it (the pairing is kept in the
  local state and logged). After that only that account counts: it keeps access after renaming, and someone who later takes
  the old nickname is refused. Removing the nickname from the list revokes access.
- **user id** (digits): exact, works without a nickname. Find it with @userinfobot, or `uv run meetings.py discover` while
  the watcher is stopped.

Contributors can add, list, delete and clear items and assemble the agenda; only the approver can send the follow-up.
The prompt is `prompts/agenda.txt` (overridable like the others). You can also use it without the bot:
`uv run meetings.py agenda add "item" | list | del 1 3 5-7 | assemble | clear | carry FILE.analysis.json`.

### Did we discuss what we planned? (agenda review and carry-over)
With the agenda step enabled, each recording is analysed **together with what was planned**:
- the agenda reserved for it (see above; not older than `agenda.max_age_days` and newer than the previous meeting), and
- the tasks and decisions of the previous meeting, by recording time; too-short (skipped) recordings do not count
  (`agenda.review_tasks`).

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
Apply the carry-over of an old analysis by hand with `uv run meetings.py agenda carry FILE.analysis.json`.

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
| `messages/<language>.json` | replaces the named texts of the bot and the follow-up (`messenger.language`, default `ru`) |

Main settings: `watch.*` (folder, file types, which audio tracks are speech, timings), `ai.speakers` / `ai.context`,
`tasks.project` / `board` / `column` / `assignees` / `create_missing`, `messenger.group` / `chat` / `approver` / `project_name` / `language`,
`agenda.enabled` / `contributors` / `max_items` / `include_open_questions` / `review` / `review_tasks` / `max_age_days`.
See [`examples/config.json`](examples/config.json) and [`defaults/config.json`](defaults/config.json).

The OBS multi-track layout is assumed by default (track 0 = mix, 1 = mic, 2 = system audio); change `watch.speech_tracks`
for other setups. Silent tracks are detected and skipped.

## Architecture
```
meetings.py   CLI + watcher (+ inbox thread)     notify.py    follow-up message + approval workflow
agenda.py     optional agenda step               plan.py      what was planned vs what happened (review, carry-over)
store.py      SQLite state, atomic writes, lock  texts.py     bot / message texts (defaults/messages/)
media.py      ffmpeg helpers                     prompts.py   prompt loading + placeholder checks
report.py     markdown report                    settings.py / secrets_config.py   layered config (reload) / secrets
adapters/
  base.py              AIAdapter · MessengerAdapter · TaskAdapter + the analysis contract
  ai_google.py  messenger_telegram.py  tasks_yougile.py      (+ __init__.py: the registry)
defaults/     config.json, prompts/, cards/, messages/
tools/        yougile_setup_key.py, install_service.py
tests/        offline test suite (no network, no keys, never touches ~/.meetings-assist)
```
**Tests:** `uv run --with pytest --with "google-genai>=3.0,<4" pytest -q tests` (also run by GitHub Actions on every push).
**Adding a system:** subclass the interface in `adapters/base.py` in `adapters/<kind>_<name>.py`, register it in
`adapters/__init__.py`, add its defaults under `<kind>.adapters.<name>` in `defaults/config.json`, and set
`ai.adapter` / `messenger.adapter` / `tasks.adapter` to its name.

## Privacy
* **The meeting audio goes to the AI provider.** On the Google Gemini API **free tier, Google may use what you send (audio,
  prompts, answers) to improve its products, and human reviewers may read it**. For private or confidential meetings
  enable billing on the Google Cloud project (paid tier: your data is not used for that) or use another AI adapter.
  See Google's Gemini API terms for the current rules.
* The analysis contains everything the AI heard, including personal topics (e.g. under "discussed but not planned");
  it is stored in `<watch dir>/transcripts/`. If that folder is synced (Yandex.Disk, Dropbox, iCloud…), your transcripts
  are in that cloud too. The follow-up message and the cards contain only the meeting's results.
* Task and message texts are generated by an AI from the audio. Review the Telegram preview before tapping Send.
* Secrets stay in `~/.meetings-assist/secrets.json` (mode 600), never in the repo.

## License
MIT, see [LICENSE](LICENSE).
