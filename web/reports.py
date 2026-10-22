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
    start_ms       INTEGER NOT NULL,   -- the clip as served (the sung phrase)
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
    status         TEXT,               -- provisional | accepted | deleted | revoked; NULL: not applied automatically
    reviewed_at    TEXT,               -- UTC, when status left provisional
    decision       TEXT,               -- the admin's review (DECISIONS); NULL: not reviewed on the site
    note           TEXT,               -- ... with an optional note
    reviewed_by    TEXT,               -- ... by whom (the Cloudflare Access email)
    review_start_ms INTEGER,           -- the times the admin set on /admin before applying, when not
    review_end_ms  INTEGER             -- the visitor's (NULL: the visitor's adjustment as it is)
)
"""
STATUSES = ("provisional", "accepted", "deleted", "revoked")
# Applied by the app until the search database includes it: provisional (a visitor's adjustment,
# not reviewed), accepted (the adjustment, reviewed), deleted (the words removed, reviewed)
APPLIED = ("provisional", "accepted", "deleted")
# An admin's review on the site (/admin) and the status it gives the report. Same decisions as
# tools/reports.py review: apply the adjustment, delete the words (aligned where nothing is
# sung), or one that changes nothing on the site (fixed elsewhere, or nothing to fix).
DECISIONS = {"apply": "accepted", "delete": "deleted", "lyrics": "revoked", "chorus": "revoked",
             "leave": "revoked", "other": "revoked", "nothing": "revoked"}


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

    def get(self, report_id):
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            return dict(row) if row else None

    def decide(self, report_id, decision, note, reviewer, times=(None, None)):
        """Record an admin's review: the decision, the status it means (DECISIONS), and for "apply"
        the times the admin set (start_ms, end_ms), or (None, None) for the visitor's."""
        if decision not in DECISIONS:
            raise ValueError(decision)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, closing(self._connect()) as conn:
            conn.execute("UPDATE reports SET decision = ?, note = ?, reviewed_by = ?, status = ?, reviewed_at = ?,"
                         " review_start_ms = ?, review_end_ms = ? WHERE id = ?",
                         (decision, note, reviewer, DECISIONS[decision], now, *times, report_id))
            conn.commit()

    def set_status(self, ids, status):
        """Record a review: accepted or revoked (tools/reports.py)."""
        if status not in STATUSES:
            raise ValueError(status)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.lock, closing(self._connect()) as conn:
            conn.executemany("UPDATE reports SET status = ?, reviewed_at = ? WHERE id = ?",
                             [(status, now, i) for i in ids])
            conn.commit()
