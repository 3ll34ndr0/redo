#!/usr/bin/env python3
"""Clip reports from visitors (¿No coincide?): download them and summarize per lyric line.

    tools/reports.py export [-o reports.json]   copy all reports from the running pod (ssh + kubectl)
    tools/reports.py summary [reports.json]      which lines are reported, and by how much they're off
    tools/reports.py summary --db reports.db     same, from a local SQLite file (e.g. a test run)

The pod's database is /reports/reports.db (web/reports.py). The server is
$EXTRACTOS_SSH, default root@fs2-usw.delightvoip.com.
"""

import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter, defaultdict

SSH = os.getenv("EXTRACTOS_SSH", "root@fs2-usw.delightvoip.com")
# Runs inside the pod (it has Python): prints every report as JSON
DUMP = ("import json,sqlite3; c=sqlite3.connect('file:/reports/reports.db?mode=ro',uri=True); "
        "c.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in c.execute('SELECT * FROM reports ORDER BY id')]))")
PROBLEM_TEXT = {"starts_late": "starts late", "starts_early": "starts early", "ends_early": "ends early",
                "ends_late": "ends late", "wrong_phrase": "wrong phrase", "unsure": "not sure what"}


def export(out):
    cmd = ["ssh", "-o", "BatchMode=yes", SSH,
           f"kubectl -n extractos exec deploy/extractos -- python -c \"{DUMP}\""]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"export failed: {r.stderr.strip()}")
    rows = json.loads(r.stdout)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"{out}: {len(rows)} reports")


def load(path=None, db=None):
    if db:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY id")]
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def seconds(ms):
    return f"{ms / 1000:+.1f} s"


def summary(rows):
    """One block per (song, line) with reports, most reported first."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["song"], r.get("line") or f"(clip at {r['start_ms'] / 1000:.1f} s)")].append(r)
    print(f"{len(rows)} reports, {len(groups)} lines, {len({s for s, _ in groups})} songs\n")
    for (song, line), reps in sorted(groups.items(), key=lambda g: -len(g[1])):
        problems = Counter(PROBLEM_TEXT.get(r["problem"], r["problem"]) for r in reps)
        print(f"{song}  ·  {line}")
        print(f"   {len(reps)} report(s): " + ", ".join(f"{n} {p}" for p, n in problems.most_common()))
        fixed = [r for r in reps if r.get("fixed_start_ms") is not None]
        if fixed:
            start = statistics.median(r["fixed_start_ms"] - r["start_ms"] for r in fixed)
            end = statistics.median(r["fixed_end_ms"] - r["end_ms"] for r in fixed)
            print(f"   {len(fixed)} with a correction, median: start {seconds(start)}, end {seconds(end)}")
        clips = Counter((r["start_ms"], r["end_ms"]) for r in reps)
        print("   clip(s): " + ", ".join(f"{s / 1000:.2f}-{e / 1000:.2f} s" + (f" ×{n}" if n > 1 else "")
                                         for (s, e), n in clips.most_common(3)))
        print()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="download all reports from the pod")
    e.add_argument("-o", "--out", default="reports.json")
    s = sub.add_parser("summary", help="summarize reports per lyric line")
    s.add_argument("file", nargs="?", default="reports.json")
    s.add_argument("--db", help="read a local reports.db instead of an export")
    args = p.parse_args()
    if args.cmd == "export":
        export(args.out)
    else:
        summary(load(args.file, args.db))


if __name__ == "__main__":
    main()
