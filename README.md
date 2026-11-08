# meetings-assist

Turns a recorded work meeting into **tasks in your task tracker** and a **short follow-up in your team chat**.

```
recording (OBS / any video or audio file)
  → AI service:   speaker-labelled transcript, summary, decisions, tasks, short follow-up
  → task tracker: one meeting card + one card per task (assigned by speaker name)
  → messenger:    private preview to you with Send / Don't send; posted to the team chat only after you approve
```

It works with three kinds of external services, each through an **adapter** (a small plugin class behind a fixed interface):

| Role | Config section | Included adapter |
|---|---|---|
| **AI service** — transcribes and analyses the recording | `ai` | Google Gemini |
| **Messenger** — the bot that shows you the preview, posts the follow-up, collects agenda items | `messenger` | Telegram |
| **Task tracker** — gets the meeting card and the task cards | `tasks` | Yougile |

Another product is one new adapter; the pipeline does not change (see [Architecture](#architecture)).
Product-specific details are collected in [Included adapters](#included-adapters).

## Requirements
macOS or Linux, [`uv`](https://docs.astral.sh/uv/), `ffmpeg` + `ffprobe`. (`brew install uv ffmpeg`)
The login-service installer is macOS (launchd) only; everything else is portable.

## Setup
```bash
mkdir -p ~/.meetings-assist && chmod 700 ~/.meetings-assist
cp examples/config.json  ~/.meetings-assist/config.json      # edit: folder, people, tracker location, chat ids
cp examples/secrets.json ~/.meetings-assist/secrets.json     # edit: keys; then chmod 600 it
uv run meetings.py check --online                            # validates everything, tests the credentials
```
Secrets live **only** in `~/.meetings-assist/secrets.json` (an environment variable of the same name overrides it).
Which keys you need and where to get them depends on the adapters: see [Included adapters](#included-adapters).

What to fill in, whatever the products:
- **`watch.dir`** — the folder your recordings land in.
- **`ai.speakers` / `ai.context`** — who usually talks (names the AI should use) and what the meetings are about.
- **`tasks.project` / `board` / `column`** — where the cards go (looked up by title); `tasks.assignees` maps a speaker
  name to the tracker account that gets the task.
- **`messenger.group` / `chat`** — the team chat (and, if the messenger has them, the topic/thread) for the follow-up;
  **`messenger.approver`** — you: only you see the preview and decide whether it is sent.
  `uv run meetings.py discover` lists the chats and people the bot can see.

## Use
```bash
uv run meetings.py process "recording.mp4"   # one file, manually
uv run meetings.py watch                     # watch the folder (watch.dir)
python3 tools/install_service.py             # (macOS) run the watcher at login; --uninstall to remove
uv run meetings.py status                    # what is done / pending
uv run meetings.py approvals                 # handle Send / Don't send once (the watcher does this itself)
uv run meetings.py cards FILE.analysis.json  # show the cards that would be created
uv run meetings.py message FILE.analysis.json
```
Each step leaves a marker next to the results (`<watch dir>/transcripts/`): `.analysis.json/.md`, `.tasks.json`,
`.message.json`, `.carryover.json`, `.skipped.json`, so every step is idempotent and a crash resumes where it stopped
(all files are written atomically). **Nothing is posted to the team chat without your approval.**

How the watcher behaves:
- **A recording is processed only when it is finished**: not written for `watch.settle_seconds` *and* not open in any
  process (`lsof`), so a paused recording is never picked up half-way (`watch.skip_open_files`).
- **The bot answers at once**, also while a recording is being analysed: approvals and agenda messages are handled by a
  separate thread that waits for the messenger instead of polling it in a busy loop.
- **Only one copy runs**: `watch`, `process` and `approvals` take a lock; a second one exits with the PID of the first.
- **Saved settings apply without a restart**: changes to `~/.meetings-assist/config.json`, `prompts/`, `cards/` or
  `messages/` are picked up within one `watch.poll_seconds`; a file with a syntax error keeps the previous settings and
  says so in the log.
- **A follow-up too long for one message** (each messenger adapter knows its limit) is cut at a line break; its full
  text goes to an extra card in the task tracker, and the message says which one.
- **Local state** (agenda list, agendas, the messenger's update position) is a SQLite database,
  `~/.meetings-assist/state.db`, deliberately outside any synced folder. Files from older versions are imported once.

## Optional: meeting agenda from the bot
Participants send **items to discuss** to the bot in a private chat. The items are kept in a list; the AI assembles the
agenda only when you ask, as a separate step, and you can rebuild it as often as you like.
```
(you → bot)  - cancel button: show the tutorial only to tap players     adds items, no AI call
             - sound budget
(you → bot)  /agenda   → the agenda: numbered points "topic — question (who proposed it)", carry-overs marked,
                         an estimated duration
(you → bot)  - one more topic       then   /agenda   again → rebuilt with everything so far
```
Every line is one item. Commands (they share the `agenda_` prefix, so they form one group in the bot's menu):

| Command | Does | Old alias |
|---|---|---|
| `/agenda` | assemble / rebuild the agenda (the latest one counts) | |
| `/agenda_add` | add items (plain text works too) | |
| `/agenda_list` | show the items, carry-overs marked | `/list` |
| `/agenda_del 1 3 5-7` | remove several items at once (bare `/agenda_del`: the next message is the numbers) | `/del` |
| `/agenda_clear` | start a new list | `/clear` |
| `/agenda_help` | how it works | `/help`, `/start` |

`uv run meetings.py bot-commands` publishes them as the bot's command menu in private chats, if the messenger has one
(`--show` just prints them). The names are one list (`COMMAND_NAMES` in `agenda.py`); every text the bot writes is in
`defaults/messages/<language>.json` (default Russian; see [Configuration](#configuration)).
A message that consists only of numbers ("1, 2, 3") is never added as an item; the bot points to `/agenda_del` instead
(add such an item explicitly with `/agenda_add 2026`).

**The list follows the meetings.** When a recording is analysed, the latest agenda assembled before it is reserved for
that meeting (a rebuild replaces the earlier version; an agenda is used by one meeting only). When you approve the
follow-up, the items that agenda was made of leave the list and whatever was not resolved comes back as carry-over items
(with a counter for repeats); items sent after the agenda was assembled stay. When you decline it (e.g. a test
recording), the list is left as it was and the agenda is free for the next recording.

**Who may use it.** Enable it in `config.json`:
`"agenda": {"enabled": true, "contributors": ["@kostya", "123456789"]}`. The approver is always allowed. Each person opens
a private chat with the bot once (most messengers do not let a bot write first). Two ways to name someone:
- **`@nickname`** (the messenger's public username): the first account that writes to the bot with that nickname is pinned
  to it (the pairing is kept in the local state and logged). After that only that account counts: it keeps access after
  renaming, and someone who later takes the old nickname is refused. Removing the nickname from the list revokes access.
  Add a nickname only when you know whose it is, and check the log line after their first message.
- **user id** (the messenger's numeric account id): exact, works without a nickname.

Contributors can add, list, delete and clear items and assemble the agenda; only the approver decides about the follow-up.
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
  become `not_mentioned` — a topic is never lost because the AI was over-optimistic;
- everything except `resolved` goes to the next agenda, together with the meeting's open questions and the tasks that
  were `blocked` or `not_mentioned` (as "status of task …" items); each carry increases a counter, and from the third time
  the agenda asks to decide on the point or drop it;
- the follow-up gets two short lists, "not discussed" and "discussed but not resolved" (you see them in the private
  preview), the meeting card in the task tracker gets the same lists, and `<stem>.analysis.md` has the full review with quotes;
- topics that were discussed but not planned stay in the local analysis only.

Prompts: `prompts/agenda_review.txt`, `prompts/task_followup.txt`. Without an agenda and without earlier tasks nothing changes.
Apply the carry-over of an old analysis by hand with `uv run meetings.py agenda carry FILE.analysis.json`.

## Configuration
`defaults/` (in the repo) holds every default; `~/.meetings-assist/` overrides it **per file**:

| File | Override |
|---|---|
| `config.json` | only the keys you change (deep merge; unknown keys are reported) |
| `prompts/*.txt` | replaces that one AI prompt (`uv run meetings.py export-defaults` copies the defaults to edit) |
| `cards/<tasks adapter>.json` | replaces the named card layouts (`meeting` / `task`) of that task tracker |
| `messages/<language>.json` | replaces the named texts of the bot and the follow-up (`messenger.language`, default `ru`) |

Main settings: `watch.*` (folder, file types, which audio tracks are speech, timings), `ai.speakers` / `ai.context`,
`tasks.project` / `board` / `column` / `assignees` / `create_missing`, `messenger.group` / `chat` / `approver` / `project_name` / `language`,
`agenda.enabled` / `contributors` / `max_items` / `include_open_questions` / `review` / `review_tasks` / `max_age_days`;
`ai.adapter` / `messenger.adapter` / `tasks.adapter` choose the adapters, `<section>.adapters.<name>` holds an adapter's
own settings. See [`examples/config.json`](examples/config.json) and [`defaults/config.json`](defaults/config.json).

Multi-track recordings: by default track 0 is assumed to be the mix, 1 the microphone and 2 the system audio (OBS's
layout); change `watch.speech_tracks` for other setups. Silent tracks are detected and skipped.

## Included adapters

### AI service: Google Gemini (`ai.adapter: "google"`)
- Secret `GEMINI_API_KEY` from Google AI Studio.
- `ai.adapters.google.models` is a fallback chain: an overloaded model is skipped for the next one. The free tier allows
  about 20 requests a day per model; enable billing for regular use (and see [Privacy](#privacy)).
- If the content filter refuses the whole recording, it is processed in pieces; refused stretches are marked as gaps.

### Messenger: Telegram (`messenger.adapter: "telegram"`)
- Secret `TELEGRAM_BOT_TOKEN` from @BotFather. Add the bot to the group (as an admin if the group uses forum topics) and
  press **Start** in a private chat with it.
- `messenger.group` = the group's chat id, `messenger.chat` = the forum topic id ("" for a normal group),
  `messenger.approver` = your user id. `uv run meetings.py discover` prints them; it only sees messages the watcher has
  not handled yet, so stop the watcher first (or ask people for their id via @userinfobot).
- One message holds at most 4096 characters; longer follow-ups take the tracker-card path described above.
- Contributors by `@nickname` use the Telegram username (optional in Telegram; a person without one is added by user id).

### Task tracker: Yougile (`tasks.adapter: "yougile"`)
- Secret `YOUGILE_API_KEY`: `python3 tools/yougile_setup_key.py` (asks for your login and password, never stores them).
- `tasks.adapters.yougile.company_id`; project, board and column are looked up by title. With `tasks.create_missing`
  a missing board or column is created (the project never is).
- `tasks.assignees` maps a speaker name to the Yougile account's e-mail. Card layout: `defaults/cards/yougile.json`.

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

**Adding a product:** subclass the interface in `adapters/base.py` in `adapters/<kind>_<name>.py`, register it in
`adapters/__init__.py`, add its defaults under `<kind>.adapters.<name>` in `defaults/config.json`, and set
`ai.adapter` / `messenger.adapter` / `tasks.adapter` to its name. The interfaces say what each method must guarantee:
for example, a messenger adapter only ever returns private messages to the bot, and a task-tracker adapter must make a
retried push never create duplicates.

## Privacy
* **The meeting audio goes to the AI service.** What the provider may do with it depends on its terms and your plan.
  With the included Gemini adapter: on the **free tier, Google may use what you send (audio, prompts, answers) to improve
  its products, and human reviewers may read it**. For private or confidential meetings enable billing on the Google
  Cloud project (paid tier: your data is not used for that) or use another AI adapter. Check the current terms.
* **The bot reads only what it needs.** Without the agenda step it asks the messenger only for your Send / Don't send
  decisions and never receives chat messages. With the agenda step it must also receive private messages to the bot;
  depending on the messenger, that can include every message the bot can see (with Telegram, the group's too, if the bot
  is an admin there). The adapter drops everything that is not a private message to the bot immediately, unread and unstored.
* **The analysis contains everything the AI heard,** including personal topics (e.g. under "discussed but not planned");
  it is stored in `<watch dir>/transcripts/`. If that folder is synced (Yandex.Disk, Dropbox, iCloud…), your transcripts are
  in that cloud too. The follow-up and the cards contain only the meeting's results.
* **The texts are written by an AI** from the audio. Review the preview before approving it.
* **Secrets** stay in `~/.meetings-assist/secrets.json` (mode 600), never in the repo.

## License
MIT, see [LICENSE](LICENSE).
