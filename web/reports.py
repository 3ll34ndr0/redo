"""Clip reports from visitors ("¿No coincide?"): a small SQLite database of their own.

The search database is read-only (a mount from the server), so reports live in a
separate file on a writable volume (REPORTS_DB; in Kubernetes /reports/reports.db on a
PersistentVolumeClaim). One app process with threads: a lock serializes the writes.
No visitor data is stored (no IP, no user agent).

Timing reports with an adjustment are PROVISIONAL fixes: the app applies them to the search
index at once (web/fixes.py), until a review marks them accepted (then they go into
text/timing_fixes.json and the next search database) or revoked (the app drops them).
"""

import re
import sqlite3
import threading
import time
from contextlib import closing

PROBLEMS = ("timing", "wrong_phrase", "unsure")
# Before 2026-10-17 the page asked which edge was off; older reports keep these values
OLD_PROBLEMS = ("starts_late", "starts_early", "ends_early", "ends_late")

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
    db_version     TEXT,               -- when the search database was built (its file time)
    first_tok      INTEGER,            -- the clip's aligned words (words.tok), found by the served times
    last_tok       INTEGER,
    first_word     TEXT,               -- ... and the words themselves, to check them before applying
    last_word      TEXT,
    status         TEXT,               -- provisional | accepted | revoked; NULL: not applied automatically
    reviewed_at    TEXT                -- UTC, when status left provisional
)
"""
STATUSES = ("provisional", "accepted", "revoked")
APPLIED = ("provisional", "accepted")    # accepted: applied until the search database includes it


class ReportStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA)
            # databases from before a column existed: add it (old reports get NULL)
            have = {r[1] for r in conn.execute("PRAGMA table_info(reports)")}
            for name, decl in re.findall(r"^\s+(\w+)\s+(INTEGER|TEXT)\b", SCHEMA, re.M):
                if name not in have:
                    conn.execute(f"ALTER TABLE reports ADD COLUMN {name} {decl}")
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

    def applied(self):
        """Reports whose adjustment the app applies (provisional or accepted), oldest first."""
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM reports WHERE status IN ({', '.join('?' * len(APPLIED))}) AND first_tok IS NOT NULL"
                " ORDER BY id", APPLIED)]

    def set_status(self, ids, status):
        """Record a review: accepted or revoked (tools/reports.py)."""
        if status not in STATUSES:
            raise ValueError(status)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, closing(self._connect()) as conn:
            conn.executemany("UPDATE reports SET status = ?, reviewed_at = ? WHERE id = ?",
                             [(status, now, i) for i in ids])
            conn.commit()
