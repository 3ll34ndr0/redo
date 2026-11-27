"""Libro de visitas: messages visitors leave by voice only (the green button at the bottom of the page).

The recording is turned into text by Orchardrun (stt.py) with the key in GUESTBOOK_ORCHARDRUN_API_KEY
(its own credit; without it, the mic button's ORCHARDRUN_API_KEY). A message is stored `pending`; an
admin approves it (/admin/messages) before its TEXT is shown on the page. The recording is never
public: only the admin listens to it, and it's deleted AUDIO_KEPT_DAYS after the message.
Same writable SQLite file as the clip reports (REPORTS_DB), tables of its own. No visitor data.

WRITTEN messages (the fallback when the voice can't be used: no key, no credit, Orchardrun down, a browser
that can't record) are untrusted input of the worst kind (spam, links, HTML/JS, SQL): they go, exactly as
typed, to a table of their own, `guestbook_quarantine`, that nothing on the public page reads. Only an
admin's "Publicar" copies a cleaned copy (one line, printable characters only) into `guestbook`, already
approved. Always bound as SQL parameters, always escaped when shown (also on /admin), never logged.
"""

import re

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
CREATE TABLE IF NOT EXISTS guestbook_quarantine (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,                     -- UTC, ISO 8601
    raw          TEXT NOT NULL,                     -- exactly as typed (never shown unescaped, never run)
    flags        TEXT NOT NULL DEFAULT '',          -- what looks suspicious in it (suspicions()), for the admin
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | published | discarded
    message_id   INTEGER,                           -- published: its row in guestbook
    reviewed_at  TEXT,
    reviewed_by  TEXT
);
"""
STATUSES = ("pending", "approved", "hidden")
DECISIONS = {"approve": "approved", "hide": "hidden"}
AUDIO_KEPT_DAYS = 30
SHOWN = 40                  # approved messages on the page, the newest
MAX_TEXT = 1000             # characters kept of a transcription (a minute of speech is ~800)
MAX_WRITTEN = 500           # characters a written message may have (more: refused, not cut)
QUARANTINE_MAX_PENDING = 500   # unreviewed written messages kept at most: a flood can't fill the small volume
QUARANTINE_DECISIONS = {"publish": "published", "discard": "discarded"}
DISCARDED_KEPT_DAYS = 30    # discarded written messages are deleted after this

# What the admin is warned about in a written message (only hints: nothing is refused or changed for them)
SUSPICIONS = {
    "enlace": re.compile(r"https?:|www\.|\b[\w-]+\.(com|net|org|info|biz|xyz|top|ru|io|me|ly|ar|site|online|click)\b", re.I),
    "contacto": re.compile(r"[\w.+-]+@[\w-]+\.\w|\+?\d[\d ()-]{8,}\d|t\.me/|wa\.me/", re.I),
    "html": re.compile(r"<\s*/?\s*[a-z!?]|&#?\w+;|\bon\w+\s*=|javascript:|data:\w+/", re.I),
    "código": re.compile(r"\{\{|\{%|\$\{|<%|`|\b(eval|exec|system|import|require)\s*\(|\$\(|\|\s*(sh|bash)\b", re.I),
    "sql": re.compile(r"\b(select|union|insert|update|delete|drop|alter|truncate)\b[\s\S]*\b(from|into|table|set|where|all)\b"
                      r"|'\s*\)*\s*(;|--|#)|/\*|;\s*(drop|select|insert|update|delete|exec|shutdown)\b"
                      r"|'\s*(or|and)\s+['\d]|\b(or|and)\s+\d+\s*=\s*\d+", re.I),
    "repetido": re.compile(r"(.)\1{9,}|(\b\w+\b)(\W+\2\b){4,}", re.I),
}


def suspicions(text):
    """The SUSPICIONS a text matches, plus "invisible" for control/format characters (bidi overrides,
    zero-width…) other than line breaks and tabs. Space separated, in a fixed order."""
    found = [name for name, pattern in SUSPICIONS.items() if pattern.search(text)]
    if any(not c.isprintable() and c not in "\n\t" for c in text):
        found.append("invisible")
    return " ".join(found)


def visible(text):
    """The text with every invisible character written as its code (U+202E…), for the admin to see it."""
    return "".join(c if c.isprintable() or c in "\n\t" else f"[U+{ord(c):04X}]" for c in text)


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

    # --- written messages: quarantine

    def quarantine(self, raw):
        """Keep a written message for an admin to examine: (id, False), or (id, True) when the same text is
        already waiting (stored once); None when QUARANTINE_MAX_PENDING are waiting. Old discarded ones go."""
        with self.lock, closing(self._connect()) as conn:
            conn.execute("DELETE FROM guestbook_quarantine WHERE status = 'discarded' AND reviewed_at < ?",
                         (_now(-DISCARDED_KEPT_DAYS * 86400),))
            same = conn.execute("SELECT id FROM guestbook_quarantine WHERE status = 'pending' AND raw = ?",
                                (raw,)).fetchone()
            if same:
                conn.commit()
                return same["id"], True
            if conn.execute("SELECT COUNT(*) FROM guestbook_quarantine WHERE status = 'pending'"
                            ).fetchone()[0] >= QUARANTINE_MAX_PENDING:
                conn.commit()
                return None
            row_id = conn.execute("INSERT INTO guestbook_quarantine (created_at, raw, flags) VALUES (?, ?, ?)",
                                  (_now(), raw, suspicions(raw))).lastrowid
            conn.commit()
        return row_id, False

    def quarantined(self):
        """Every written message, newest first (for /admin)."""
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM guestbook_quarantine ORDER BY id DESC")]

    def quarantined_one(self, row_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM guestbook_quarantine WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None

    def release(self, row_id, decision, admin):
        """publish → a cleaned copy goes to guestbook, approved (once: publishing again reuses it, and
        shows it again if it was hidden); discard → not published (a published copy is hidden).
        The new status, or None if there's no such row (or nothing printable to publish)."""
        with self.lock, closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM guestbook_quarantine WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                return None
            message_id, now = row["message_id"], _now()
            if decision == "publish":
                text = clean(row["raw"], MAX_TEXT)
                if not text:
                    return None
                if message_id is None:
                    message_id = conn.execute(
                        "INSERT INTO guestbook (created_at, text, status, reviewed_at, reviewed_by)"
                        " VALUES (?, ?, 'approved', ?, ?)", (row["created_at"], text, now, admin)).lastrowid
                else:
                    conn.execute("UPDATE guestbook SET status = 'approved', reviewed_at = ?, reviewed_by = ?"
                                 " WHERE id = ?", (now, admin, message_id))
            elif message_id is not None:
                conn.execute("UPDATE guestbook SET status = 'hidden', reviewed_at = ?, reviewed_by = ? WHERE id = ?",
                             (now, admin, message_id))
            status = QUARANTINE_DECISIONS[decision]
            conn.execute("UPDATE guestbook_quarantine SET status = ?, message_id = ?, reviewed_at = ?, reviewed_by = ?"
                         " WHERE id = ?", (status, message_id, now, admin, row_id))
            conn.commit()
        return status
