#!/usr/bin/env python3
"""Clip reports from visitors (¿No coincide?): download them and summarize per lyric line.

    tools/reports.py export [-o reports.json]   copy all reports from the running pod (ssh + kubectl)
    tools/reports.py summary [reports.json]      which lines are reported, and by how much they're off
    tools/reports.py summary --db reports.db     same, from a local SQLite file (e.g. a test run)
    tools/reports.py review [reports.json]       go through the reports not reviewed yet: clip URLs
                                                 (as served / as the visitor adjusted it), record a decision
    tools/reports.py review --list [--all]       only print them (--all: reviewed ones too)

Review decisions are kept in reports-reviewed.json next to the export (not in git: notes may
quote lyrics), keyed by report id + creation time, so a new export only shows new reports.

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
import time
from collections import Counter, defaultdict
from urllib.parse import quote

SSH = os.getenv("EXTRACTOS_SSH", "root@fs2-usw.delightvoip.com")
SITE = os.getenv("EXTRACTOS_URL", "https://extractos.marso.ar").rstrip("/")
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


DECISIONS = {"l": "lyrics fixed", "c": "chorus added", "t": "timing off, not fixed yet",
             "o": "other fix", "n": "nothing wrong"}


def clip_url(song, start_ms, end_ms):
    return f"{SITE}/clip/{quote(song)}/{start_ms}-{end_ms}.mp3"


def report_key(r):
    """Ids restart if the server's database is ever lost: the creation time keeps keys unique."""
    return f"{r['id']}@{r['created_at']}"


def load_reviewed(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_reviewed(path, reviewed):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reviewed, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def show(r, done=None):
    problem = PROBLEM_TEXT.get(r["problem"], r["problem"])
    print(f"#{r['id']}  {r['created_at']}  {r['song']}  ·  {problem}")
    if r.get("line"):
        print(f"   line:     {r['line']}")
    if r.get("query"):
        print(f"   searched: {r['query']}")
    print(f"   served:   {clip_url(r['song'], r['start_ms'], r['end_ms'])}")
    if r.get("fixed_start_ms") is not None:
        print(f"   adjusted: {clip_url(r['song'], r['fixed_start_ms'], r['fixed_end_ms'])}"
              f"   (start {seconds(r['fixed_start_ms'] - r['start_ms'])}, end {seconds(r['fixed_end_ms'] - r['end_ms'])})")
    if r.get("db_version"):
        print(f"   database: {r['db_version']}")
    if done:
        print(f"   reviewed: {done['decision']} ({done['at']})" + (f": {done['note']}" if done.get("note") else ""))


def review(rows, reviewed_path, list_only=False, show_all=False):
    reviewed = load_reviewed(reviewed_path)
    todo = [r for r in rows if show_all or report_key(r) not in reviewed]
    print(f"{len(rows)} reports, {len(rows) - sum(report_key(r) not in reviewed for r in rows)} reviewed, "
          f"{sum(report_key(r) not in reviewed for r in rows)} to review  ({reviewed_path})\n")
    if list_only or not todo:
        for r in todo:
            show(r, reviewed.get(report_key(r)))
            print()
        return
    keys = "  ".join(f"[{k}] {v}" for k, v in DECISIONS.items())
    for n, r in enumerate(todo, 1):
        print(f"── {n}/{len(todo)}")
        show(r, reviewed.get(report_key(r)))
        while True:
            try:
                answer = input(f"   {keys}  [s] skip  [q] quit\n   > ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in DECISIONS or answer in ("s", "q"):
                break
        if answer == "q":
            break
        if answer == "s":
            print()
            continue
        try:
            note = input("   note (optional): ").strip()
        except EOFError:
            note = ""
        reviewed[report_key(r)] = {"decision": DECISIONS[answer], "note": note,
                                   "at": time.strftime("%Y-%m-%d %H:%M")}
        save_reviewed(reviewed_path, reviewed)      # after each answer: quitting loses nothing
        print()
    left = sum(report_key(r) not in reviewed for r in rows)
    print(f"{left} left to review")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="download all reports from the pod")
    e.add_argument("-o", "--out", default="reports.json")
    s = sub.add_parser("summary", help="summarize reports per lyric line")
    s.add_argument("file", nargs="?", default="reports.json")
    s.add_argument("--db", help="read a local reports.db instead of an export")
    r = sub.add_parser("review", help="listen to each report's clips and record a decision")
    r.add_argument("file", nargs="?", default="reports.json")
    r.add_argument("--list", action="store_true", help="only print, don't ask")
    r.add_argument("--all", action="store_true", help="include reports already reviewed")
    r.add_argument("--reviewed", help="decisions file (default: reports-reviewed.json next to the export)")
    args = p.parse_args()
    if args.cmd == "export":
        export(args.out)
    elif args.cmd == "summary":
        summary(load(args.file, args.db))
    else:
        reviewed = args.reviewed or os.path.join(os.path.dirname(os.path.abspath(args.file)), "reports-reviewed.json")
        review(load(args.file), reviewed, args.list, args.all)


if __name__ == "__main__":
    main()
