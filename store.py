"""Local state in SQLite (~/.meetings-assist/state.db) + atomic file writes + the single-instance lock. Stdlib only.

Why SQLite: the bot thread and the watcher touch the same state at the same time, and a crash or a sync client must never
leave a half-written file behind. Every operation is one transaction; a new connection per call keeps it thread-safe.
The database is deliberately NOT next to the recordings: a synced folder (Yandex.Disk, Dropbox...) can corrupt SQLite.
Markers, transcripts and reports stay as files next to the recordings (written atomically with write_atomic).

Tables
  items     the agenda list (id, text, sender, carried counter, where a carry-over came from)
  pending   per sender: "the next message answers a command" (e.g. the numbers for /agenda_del)
  agendas   every assembled agenda: points (with the item ids they came from), state: open | replaced | reserved | used
  carry     per meeting: the leftovers waiting for the follow-up decision (pending | applied | discarded)
  kv        small adapter state (e.g. the Telegram update offset)
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, sender_name TEXT DEFAULT '', sender_id TEXT DEFAULT '',
    at TEXT DEFAULT '', carried INTEGER DEFAULT 0, from_meeting TEXT DEFAULT '', why TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS pending (sender_id TEXT PRIMARY KEY, what TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agendas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, points TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open', meeting TEXT);
CREATE TABLE IF NOT EXISTS carry (
    stem TEXT PRIMARY KEY, status TEXT NOT NULL, points TEXT NOT NULL, agenda_id INTEGER, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename: readers see the old or the new content, never half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)          # executescript() manages its own transaction
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        finally:
            db.close()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)   # autocommit; BEGIN IMMEDIATE below
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    # ---- kv -------------------------------------------------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        with self._db() as db:
            r = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    # ---- items ----------------------------------------------------------------------------------
    @staticmethod
    def _item(r) -> dict:
        return {"id": r["id"], "text": r["text"], "from": r["sender_name"], "sender_id": r["sender_id"], "at": r["at"],
                "carried": r["carried"], "from_meeting": r["from_meeting"], "why": r["why"]}

    def items(self) -> list[dict]:
        with self._db() as db:
            return [self._item(r) for r in db.execute("SELECT * FROM items ORDER BY id")]

    def add_items(self, rows: list[dict], limit: int | None = None) -> tuple[int, int]:
        """Append rows ({text, from, sender_id, carried, from_meeting, why}); with `limit` only up to that many items
        in total. Returns (added, total)."""
        with self._db() as db:
            have = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            take = rows if limit is None else rows[:max(0, limit - have)]
            for x in take:
                db.execute("INSERT INTO items(text, sender_name, sender_id, at, carried, from_meeting, why) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (x["text"], x.get("from", ""), x.get("sender_id", ""), time.strftime("%Y-%m-%d %H:%M"),
                            x.get("carried", 0), x.get("from_meeting", ""), x.get("why", "")))
            return len(take), have + len(take)

    def delete_items(self, ids: list[int]) -> None:
        with self._db() as db:
            db.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids])

    def clear_items(self) -> int:
        with self._db() as db:
            return db.execute("DELETE FROM items").rowcount

    # ---- pending question per sender --------------------------------------------------------------
    def pending(self, sender_id: str) -> str | None:
        with self._db() as db:
            r = db.execute("SELECT what FROM pending WHERE sender_id=?", (sender_id,)).fetchone()
        return r["what"] if r else None

    def set_pending(self, sender_id: str, what: str | None) -> None:
        with self._db() as db:
            if what:
                db.execute("INSERT INTO pending VALUES(?, ?) ON CONFLICT(sender_id) DO UPDATE SET what=excluded.what",
                           (sender_id, what))
            else:
                db.execute("DELETE FROM pending WHERE sender_id=?", (sender_id,))

    # ---- agendas ----------------------------------------------------------------------------------
    def save_agenda(self, points: list[dict], created: float | None = None) -> int:
        """A new assembly replaces every earlier agenda that no meeting has taken yet (a rebuild is not a second agenda)."""
        with self._db() as db:
            db.execute("UPDATE agendas SET state='replaced' WHERE state='open'")
            cur = db.execute("INSERT INTO agendas(created, points) VALUES(?, ?)",
                             (created if created is not None else time.time(), json.dumps(points, ensure_ascii=False)))
            return cur.lastrowid

    @staticmethod
    def _agenda(r) -> dict:
        return {"id": r["id"], "created": r["created"], "points": json.loads(r["points"]), "state": r["state"],
                "meeting": r["meeting"]}

    def agenda(self, agenda_id: int) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM agendas WHERE id=?", (agenda_id,)).fetchone()
        return self._agenda(r) if r else None

    def agendas(self) -> list[dict]:
        with self._db() as db:
            return [self._agenda(r) for r in db.execute("SELECT * FROM agendas ORDER BY id")]

    def pick_agenda(self, stem: str, latest_ok: float, earliest_ok: float) -> dict | None:
        """The agenda for this meeting: one it already reserved, else the newest open one made in [earliest, latest]."""
        with self._db() as db:
            r = db.execute("SELECT * FROM agendas WHERE meeting=? ORDER BY id DESC LIMIT 1", (stem,)).fetchone() or \
                db.execute("SELECT * FROM agendas WHERE state='open' AND created<=? AND created>=? ORDER BY created DESC, id DESC "
                           "LIMIT 1", (latest_ok, earliest_ok)).fetchone()
        return self._agenda(r) if r else None

    def set_agenda_state(self, agenda_id: int, state: str, meeting: str | None) -> None:
        with self._db() as db:
            db.execute("UPDATE agendas SET state=?, meeting=? WHERE id=?", (state, meeting, agenda_id))

    # ---- carry-over waiting for the follow-up decision -----------------------------------------------
    def set_carry(self, stem: str, status: str, points: list[dict], agenda_id: int | None) -> None:
        with self._db() as db:
            db.execute("INSERT INTO carry VALUES(?,?,?,?,?) ON CONFLICT(stem) DO UPDATE SET status=excluded.status, "
                       "points=excluded.points, agenda_id=excluded.agenda_id, at=excluded.at",
                       (stem, status, json.dumps(points, ensure_ascii=False), agenda_id, time.time()))

    def carry(self, stem: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM carry WHERE stem=?", (stem,)).fetchone()
        return {"stem": r["stem"], "status": r["status"], "points": json.loads(r["points"]), "agenda_id": r["agenda_id"]} \
            if r else None

    # ---- one-time import of the old file-based state ------------------------------------------------
    def import_legacy(self, out_dir: Path) -> list[str]:
        """agenda.json / agendas/*.json / .telegram_offset from earlier versions -> the database (files renamed *.imported)."""
        done = []
        f = out_dir / "agenda.json"
        if f.exists():
            st = json.loads(f.read_text())
            self.add_items([{"text": x["text"], "from": x.get("from", ""), "sender_id": x.get("sender_id", ""),
                             "carried": x.get("carried", 0), "from_meeting": x.get("from_meeting", ""), "why": x.get("why", "")}
                            for x in st.get("items", [])])
            for sid, what in (st.get("pending") or {}).items():
                self.set_pending(sid, what)
            f.rename(f.with_suffix(".json.imported"))
            done.append(f"{len(st.get('items', []))} agenda items")
        folder = out_dir / "agendas"
        for p in sorted(folder.glob("*.json")) if folder.exists() else []:
            try:
                a = json.loads(p.read_text())
            except ValueError:
                continue
            aid = self.save_agenda(a.get("points") or [], a.get("created", p.stat().st_mtime))
            if a.get("used_by"):
                self.set_agenda_state(aid, "used", a["used_by"])
            p.rename(p.with_suffix(".json.imported"))
            done.append(f"agenda {p.name}")
        off = out_dir / ".telegram_offset"
        if off.exists():
            self.set("telegram.offset", off.read_text().strip())
            off.rename(off.with_name(".telegram_offset.imported"))
            done.append("telegram offset")
        return done


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """Only one process may poll the messenger / process recordings at a time (a second one would read the same bot
    updates and analyse the same file twice). The OS releases the lock when the process dies."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            other = os.read(fd, 64).decode(errors="replace").strip() or "?"
            os.close(fd)
            raise AlreadyRunning(f"another meetings-assist is already running (pid {other}). Stop it first, e.g. "
                                 f"python3 tools/install_service.py --uninstall, or use that one.") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
