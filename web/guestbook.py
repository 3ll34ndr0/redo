"""Libro de visitas: messages visitors leave by voice only (the green button at the bottom of the page).

The recording is turned into text by Orchardrun (stt.py) with the key in GUESTBOOK_ORCHARDRUN_API_KEY
(its own credit; without it, the mic button's ORCHARDRUN_API_KEY). A message is stored `pending`; an
admin approves it (/admin/messages) before its TEXT is shown on the page. The recording is never
public: only the admin listens to it, and it's deleted AUDIO_KEPT_DAYS after the message.
Same writable SQLite file as the clip reports (REPORTS_DB), tables of its own. No visitor data.
"""

import os
import sqlite3
import threading
import time
from contextlib import closing

import stt

SCHEMA = """
CREATE TABLE IF NOT EXISTS guestbook (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,                     -- UTC, ISO 8601
    text         TEXT NOT NULL,                     -- what Orchardrun heard
    seconds      REAL,                              -- the recording's length, as the page measured it
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | hidden
    reviewed_at  TEXT,
    reviewed_by  TEXT                               -- the admin's Access email
);
CREATE TABLE IF NOT EXISTS guestbook_audio (
    message_id   INTEGER PRIMARY KEY,
    type         TEXT NOT NULL,                     -- the recording's media type (audio/webm, audio/mp4…)
    data         BLOB NOT NULL
);
"""
STATUSES = ("pending", "approved", "hidden")
DECISIONS = {"approve": "approved", "hide": "hidden"}
AUDIO_KEPT_DAYS = 30
SHOWN = 40                  # approved messages on the page, the newest
MAX_TEXT = 1000             # characters kept of a transcription (a minute of speech is ~800)


def clean(value, limit):
    """One line of printable text (no control characters), at most `limit` long."""
    text = "".join(c if c.isprintable() else " " for c in value) if isinstance(value, str) else ""
    return " ".join(text.split())[:limit].strip()


def key_env():
    """The env variable whose Orchardrun key the guestbook uses."""
    return "GUESTBOOK_ORCHARDRUN_API_KEY" if os.getenv("GUESTBOOK_ORCHARDRUN_API_KEY") else stt.KEY_ENV


def _now(offset_s=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


class Guestbook:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, text, seconds, audio, audio_type):
        """Store a message (pending) and its recording; recordings older than AUDIO_KEPT_DAYS go."""
        with self.lock, closing(self._connect()) as conn:
            message_id = conn.execute("INSERT INTO guestbook (created_at, text, seconds) VALUES (?, ?, ?)",
                                      (_now(), text[:MAX_TEXT], seconds)).lastrowid
            conn.execute("INSERT INTO guestbook_audio (message_id, type, data) VALUES (?, ?, ?)",
                         (message_id, audio_type, audio))
            conn.execute("DELETE FROM guestbook_audio WHERE message_id IN"
                         " (SELECT id FROM guestbook WHERE created_at < ?)", (_now(-AUDIO_KEPT_DAYS * 86400),))
            conn.commit()
        return message_id

    def shown(self, limit=SHOWN):
        """The approved messages for the page, oldest first (a chat: the newest next to the button)."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, created_at, text FROM guestbook WHERE status = 'approved'"
                                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def all(self):
        """Every message, newest first, with whether its recording is still kept (for /admin)."""
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT g.*, a.message_id IS NOT NULL AS has_audio FROM guestbook g"
                " LEFT JOIN guestbook_audio a ON a.message_id = g.id ORDER BY g.id DESC")]

    def get(self, message_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM guestbook WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def audio(self, message_id):
        """(media type, bytes) of a message's recording, or None."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT type, data FROM guestbook_audio WHERE message_id = ?", (message_id,)).fetchone()
        return (row["type"], row["data"]) if row else None

    def decide(self, message_id, decision, admin):
        """approve → shown on the page; hide → not (again). False if there's no such message."""
        with self.lock, closing(self._connect()) as conn:
            n = conn.execute("UPDATE guestbook SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
                             (DECISIONS[decision], _now(), admin, message_id)).rowcount
            conn.commit()
        return n == 1
