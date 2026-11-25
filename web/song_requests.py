"""Songs visitors ask for ("Pedí la canción", shown when a search finds nothing exact).

Kept in the same writable SQLite file as the clip reports (REPORTS_DB), in a table of its own.
No visitor data is stored. Asking again for a song still open only raises its `times`.
Read and close them with tools/song_requests.py.
"""

import sqlite3
import threading
import time
from contextlib import closing

import textnorm

SCHEMA = """
CREATE TABLE IF NOT EXISTS song_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,             -- UTC, ISO 8601
    last_at     TEXT NOT NULL,             -- the last time it was asked for
    times       INTEGER NOT NULL DEFAULT 1,
    title       TEXT NOT NULL,             -- as the (first) visitor wrote them
    artist      TEXT NOT NULL,             -- '' = not given
    key         TEXT NOT NULL,             -- normalized title|artist: the same song asked again
    query       TEXT,                      -- what was searched the first time
    status      TEXT NOT NULL DEFAULT 'open',   -- open | published | rejected
    closed_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS song_requests_open ON song_requests (key) WHERE status = 'open';
"""


def clean(value, limit):
    """One line of printable text (no control characters: tools print it), at most `limit` long."""
    text = "".join(c if c.isprintable() else " " for c in value) if isinstance(value, str) else ""
    return " ".join(text.split())[:limit].strip()


def song_key(title, artist):
    """What makes two requests the same song; '' when the title has no letters or digits."""
    words = textnorm.words(title)
    return f"{' '.join(words)}|{' '.join(textnorm.words(artist))}" if words else ""


class SongRequests:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, title, artist, query):
        """Store a request or count it on the open one for the same song. Returns (id, new)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        key = song_key(title, artist)
        with self.lock, closing(self._connect()) as conn:
            row = conn.execute("SELECT id FROM song_requests WHERE key = ? AND status = 'open'", (key,)).fetchone()
            if row:
                conn.execute("UPDATE song_requests SET times = times + 1, last_at = ? WHERE id = ?", (now, row[0]))
                request_id = row[0]
            else:
                request_id = conn.execute(
                    "INSERT INTO song_requests (created_at, last_at, title, artist, key, query) VALUES (?, ?, ?, ?, ?, ?)",
                    (now, now, title, artist, key, query)).lastrowid
            conn.commit()
        return request_id, row is None

    def all(self):
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM song_requests ORDER BY id")]
