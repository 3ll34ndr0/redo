"""Clip reports from visitors ("¿No coincide?"): a small SQLite database of their own.

The search database is read-only (a mount from the server), so reports live in a
separate file on a writable volume (REPORTS_DB; in Kubernetes /reports/reports.db on a
PersistentVolumeClaim). One app process with threads: a lock serializes the writes.
No visitor data is stored (no IP, no user agent).
"""

import sqlite3
import threading
import time
from contextlib import closing

PROBLEMS = ("starts_late", "starts_early", "ends_early", "wrong_phrase")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,      -- UTC, ISO 8601
    song           TEXT NOT NULL,
    start_ms       INTEGER NOT NULL,   -- the clip as served (sung phrase, before padding)
    end_ms         INTEGER NOT NULL,
    problem        TEXT NOT NULL,      -- one of PROBLEMS
    fixed_start_ms INTEGER,            -- the visitor's adjustment, if any
    fixed_end_ms   INTEGER,
    query          TEXT,               -- what was searched
    line           TEXT,               -- the lyric line shown on the card
    db_version     TEXT                -- when the search database was built (its file time)
)
"""


class ReportStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA)
            conn.commit()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, report):
        """Store one validated report; returns its id."""
        row = dict(report, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        cols = ", ".join(row)
        with self.lock, closing(self._connect()) as conn:
            cur = conn.execute(f"INSERT INTO reports ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
            conn.commit()
            return cur.lastrowid

    def all(self):
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY id")]
