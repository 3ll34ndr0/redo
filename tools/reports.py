#!/usr/bin/env python3
"""Clip reports from visitors (¿No coincide?): download them and summarize per lyric line.

    tools/reports.py export [-o reports.json]   copy all reports from the running pod (ssh + kubectl)
    tools/reports.py summary [reports.json]      which lines are reported, and by how much they're off
    tools/reports.py summary --db reports.db     same, from a local SQLite file (e.g. a test run)
    tools/reports.py review [reports.json]       go through the reports not reviewed yet: clip URLs
                                                 (as served / as the visitor adjusted it), record a decision
    tools/reports.py review --list [--all]       only print them (--all: reviewed ones too)
    tools/reports.py review --id 3 [--id 4]      review those reports again
    tools/reports.py pending [reports.json]      provisional fixes (live on the site) not reviewed yet
    tools/reports.py sync [--dry-run]            send the reviews' statuses to the pod (accepted/revoked)

Each site has its own files: production reports.json + reports-reviewed.json, staging
(EXTRACTOS_NAMESPACE=unaudio-dev) reports-unaudio-dev.json + reports-reviewed-unaudio-dev.json,
so a sync can't send one site's decisions to the other by default.

Review decisions are kept in reports-reviewed.json next to the export (not in git: notes may
quote lyrics), keyed by report id + creation time, so a new export only shows new reports.

Two decisions change the search database (via lyrics/text/timing_fixes.json, private repo,
applied by lyrics/build_db.py; then rsync the DB):
  [a] apply the visitor's adjustment: the clip's phrase gets the new start/end (web/fixes.py fit_span)
  [d] delete the clip's words: they were aligned where they aren't sung
The clip's words are the ones the server stored with the report (first_tok..last_tok); for
older reports, found in the local web/redondos.db by the served times.

Provisional fixes: the site applies a timing report's adjustment at once (web/reports.py).
A review decides its status: [a] = accepted (stays applied until the rebuilt database has it),
anything else = revoked (the site drops it). `sync` sends those statuses to the pod; until
then the provisional fix stays live.

The pod's database is /reports/reports.db (web/reports.py). The server is
$EXTRACTOS_SSH, default root@fs2-usw.delightvoip.com; the namespace $EXTRACTOS_NAMESPACE, default
extractos (staging: unaudio-dev, with UNAUDIO_URL=https://unaudio-dev.marso.ar).
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))
import fixes  # noqa: E402  (web/fixes.py: the same code as the app and build_db.py)

SSH = os.getenv("EXTRACTOS_SSH", "root@fs2-usw.delightvoip.com")
NAMESPACE = os.getenv("EXTRACTOS_NAMESPACE", "extractos")
_SITE_SUFFIX = "" if NAMESPACE == "extractos" else f"-{NAMESPACE}"
EXPORT = f"reports{_SITE_SUFFIX}.json"                 # default file names, one pair per site
REVIEWED = f"reports-reviewed{_SITE_SUFFIX}.json"
SITE = os.getenv("UNAUDIO_URL", "https://unaudio.marso.ar").rstrip("/")
# Runs inside the pod (it has Python): prints every report as JSON
DUMP = ("import json,sqlite3; c=sqlite3.connect('file:/reports/reports.db?mode=ro',uri=True); "
        "c.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in c.execute('SELECT * FROM reports ORDER BY id')]))")
PROBLEM_TEXT = {"timing": "badly cut", "starts_late": "starts late", "starts_early": "starts early", "ends_early": "ends early",
                "ends_late": "ends late", "wrong_phrase": "wrong phrase", "unsure": "not sure what"}


# Runs inside the pod: sets the statuses (CHANGES = [[id, created_at, status], ...]), prints how many
SET_STATUS = """import json, sqlite3, time
changes = json.loads(%r)
now = time.strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ", time.gmtime())
c = sqlite3.connect("/reports/reports.db", timeout=10)
n = sum(c.execute("UPDATE reports SET status = ?, reviewed_at = ? WHERE id = ? AND created_at = ?",
                  (status, now, i, created)).rowcount for i, created, status in changes)
c.commit()
print(n)
"""


def in_pod(command, stdin=None):
    cmd = ["ssh", "-o", "BatchMode=yes", SSH, f"kubectl -n {NAMESPACE} exec {'-i ' if stdin else ''}deploy/extractos -- {command}"]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True)


def export(out):
    r = in_pod(f"python -c \"{DUMP}\"")
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
    if not os.path.exists(path):
        sys.exit(f"{path} not found: download the reports first (tools/reports.py export, {NAMESPACE})")
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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH_DB = os.path.join(ROOT, "web", "redondos.db")
# Corrections applied by lyrics/build_db.py; in the PRIVATE lyrics repo (entries quote words)
FIXES = os.path.join(ROOT, "lyrics", "text", "timing_fixes.json")
DECISIONS = {"a": "apply the adjustment", "d": "delete the words", "l": "lyrics fixed", "c": "chorus added",
             "t": "timing off, leave it", "o": "other fix", "n": "nothing wrong"}


def clip_url(song, start_ms, end_ms):
    return f"{SITE}/clip/{quote(song)}/{start_ms}-{end_ms}.mp3"


def report_key(r):
    return fixes.report_key(r)


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
    if r.get("status"):
        print(f"   status:   {r['status']}" + (" (live on the site until revoked)" if r["status"] == "provisional" else ""))
    if done:
        print(f"   reviewed: {done['decision']} ({done['at']})" + (f": {done['note']}" if done.get("note") else ""))


def phrase_words(conn, song, start_ms, end_ms):
    """The aligned words a clip was made of: [(tok, word, start, end), ...] or None.

    The app builds clip URLs from int(start * 1000) of the phrase's first word and
    int(end * 1000) of its last (web/app.py), so the served times identify them, as long
    as the local search database has the same timings as the server's.
    """
    rows = conn.execute("SELECT tok, orig, start, end FROM words WHERE song = ? GROUP BY tok ORDER BY tok",
                        (song,)).fetchall()
    for a, (tok, _, s, _) in enumerate(rows):
        if abs(int(s * 1000) - start_ms) <= 1:
            for b in range(a, len(rows)):
                if abs(int(rows[b][3] * 1000) - end_ms) <= 1:
                    return rows[a:b + 1]
    return None


def report_words(conn, r):
    """The clip's words [(tok, word, start, end), ...] or None: the ones the server stored with
    the report (all of them from the local database when it still has the same words there,
    else only the first and last, times None), or for older reports found by the served times."""
    if r.get("first_tok") is None:
        return phrase_words(conn, r["song"], r["start_ms"], r["end_ms"]) if conn else None
    if conn:
        rows = conn.execute("SELECT tok, orig, start, end FROM words WHERE song = ? AND tok BETWEEN ? AND ?"
                            " GROUP BY tok ORDER BY tok", (r["song"], r["first_tok"], r["last_tok"])).fetchall()
        if rows and (rows[0][:2], rows[-1][:2]) == ((r["first_tok"], r["first_word"]), (r["last_tok"], r["last_word"])):
            return rows
    ends = [(r["first_tok"], r["first_word"], None, None)]
    return ends + ([(r["last_tok"], r["last_word"], None, None)] if r["last_tok"] != r["first_tok"] else [])


def status_after(r, answer):
    """The report's status once this review reaches the site (sync): None = unchanged. Only
    reports with their words stored (first_tok) can be applied by the site: others keep none."""
    if r.get("first_tok") is None:
        return None
    if answer == "a":
        return "accepted"
    return "revoked" if r.get("status") in ("provisional", "accepted") else None


def load_fixes(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_fix(r, words, action, note):
    """One entry of timing_fixes.json (see lyrics/build_db.py: apply_fixes)."""
    fix = {"report": report_key(r), "song": r["song"], "action": action,
           "first": words[0][0], "last": words[-1][0], "first_word": words[0][1], "last_word": words[-1][1]}
    if action == "adjust":
        if r["fixed_start_ms"] != r["start_ms"]:
            fix["start"] = r["fixed_start_ms"] / 1000
        if r["fixed_end_ms"] != r["end_ms"]:
            fix["end"] = r["fixed_end_ms"] / 1000
    fix.update(added=time.strftime("%Y-%m-%d"), note=note)
    return fix


def review(rows, reviewed_path, list_only=False, show_all=False, ids=(), db=SEARCH_DB, fixes_path=FIXES):
    reviewed = load_reviewed(reviewed_path)
    pending = sum(report_key(r) not in reviewed for r in rows)
    if ids:
        todo = [r for r in rows if r["id"] in ids]
    else:
        todo = [r for r in rows if show_all or report_key(r) not in reviewed]
    print(f"{len(rows)} reports, {len(rows) - pending} reviewed, {pending} to review  ({reviewed_path})\n")
    if list_only or not todo:
        for r in todo:
            show(r, reviewed.get(report_key(r)))
            print()
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if os.path.exists(db) else None
    for n, r in enumerate(todo, 1):
        print(f"── {n}/{len(todo)}")
        show(r, reviewed.get(report_key(r)))
        words = report_words(conn, r)
        if words:
            text = " ".join(w for _, w, _, _ in words) if words[0][2] is not None else " … ".join(w for _, w, _, _ in words)
            print(f"   words:    {words[0][0]}-{words[-1][0]} in the alignment: {text}")
        else:
            print(f"   words:    not found in {db} (re-aligned since the report?): [a]/[d] unavailable")
        choices = {k: v for k, v in DECISIONS.items()
                   if (k != "a" or (words and r.get("fixed_start_ms") is not None)) and (k != "d" or words)}
        keys = "  ".join(f"[{k}] {v}" for k, v in choices.items())
        while True:
            try:
                answer = input(f"   {keys}  [s] skip  [q] quit\n   > ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in choices or answer in ("s", "q"):
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
        before = load_fixes(fixes_path)
        kept = [f for f in before if f["report"] != report_key(r)]     # a re-review replaces (or drops) its fix
        if answer in ("a", "d"):
            kept.append(make_fix(r, words, "adjust" if answer == "a" else "delete", note))
        if kept != before:
            os.makedirs(os.path.dirname(fixes_path), exist_ok=True)
            save_reviewed(fixes_path, kept)
            print(f"   → {fixes_path}: rebuild with lyrics/build_db.py, rsync the DB, commit lyrics/text")
        reviewed[report_key(r)] = {"decision": DECISIONS[answer], "note": note,
                                   "at": time.strftime("%Y-%m-%d %H:%M"), "status": status_after(r, answer)}
        save_reviewed(reviewed_path, reviewed)      # after each answer: quitting loses nothing
        if reviewed[report_key(r)]["status"]:
            print(f"   → {reviewed[report_key(r)]['status']} on the site after `tools/reports.py sync`")
        print()
    left = sum(report_key(r) not in reviewed for r in rows)
    print(f"{left} left to review")
    if status_changes(rows, reviewed):
        print("statuses to send to the site: tools/reports.py sync")


def status_changes(rows, reviewed):
    """[(report, status)] whose reviewed status the site doesn't have yet."""
    out = []
    for r in rows:
        want = reviewed.get(report_key(r), {}).get("status")
        if want and r.get("status") != want and r.get("first_tok") is not None:
            out.append((r, want))
    return out


def pending(rows, reviewed_path):
    """Provisional fixes live on the site that nobody has reviewed: oldest first."""
    reviewed = load_reviewed(reviewed_path)
    todo = [r for r in rows if r.get("status") == "provisional" and report_key(r) not in reviewed]
    print(f"{len(todo)} provisional fix(es) not reviewed (live on the site)\n")
    for r in todo:
        show(r)
        print()


def sync(path, reviewed_path, dry_run=False):
    """Send the reviews' statuses to the pod, then record them in the export (so a second sync
    sends nothing)."""
    rows = load(path)
    changes = status_changes(rows, load_reviewed(reviewed_path))
    for r, status in changes:
        print(f"#{r['id']}  {r['song']}  {r.get('status') or '-'} → {status}")
    if not changes:
        print("nothing to send")
        return
    if dry_run:
        print(f"{len(changes)} change(s), not sent (--dry-run)")
        return
    result = in_pod("python -", SET_STATUS % json.dumps([[r["id"], r["created_at"], st] for r, st in changes]))
    if result.returncode != 0:
        sys.exit(f"sync failed: {result.stderr.strip()}")
    updated = int(result.stdout.strip() or 0)
    print(f"{updated} of {len(changes)} report(s) updated on the site ({NAMESPACE})"
          + ("" if updated == len(changes) else ": some reports are no longer there? export again"))
    if updated == len(changes):
        for r, status in changes:
            r["status"] = status
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="download all reports from the pod")
    e.add_argument("-o", "--out", default=EXPORT)
    s = sub.add_parser("summary", help="summarize reports per lyric line")
    s.add_argument("file", nargs="?", default=EXPORT)
    s.add_argument("--db", help="read a local reports.db instead of an export")
    r = sub.add_parser("review", help="listen to each report's clips and record a decision")
    r.add_argument("file", nargs="?", default=EXPORT)
    r.add_argument("--list", action="store_true", help="only print, don't ask")
    r.add_argument("--all", action="store_true", help="include reports already reviewed")
    r.add_argument("--reviewed", help=f"decisions file (default: {REVIEWED} next to the export)")
    r.add_argument("--id", type=int, action="append", default=[], help="review this report again (repeatable)")
    r.add_argument("--search-db", default=SEARCH_DB, help="search database to find the clip's words")
    r.add_argument("--fixes", default=FIXES, help="timing corrections file ([a] and [d] write it)")
    for name, help in (("pending", "provisional fixes not reviewed yet"),
                       ("sync", "send the reviews' statuses (accepted/revoked) to the site")):
        c = sub.add_parser(name, help=help)
        c.add_argument("file", nargs="?", default=EXPORT)
        c.add_argument("--reviewed", help=f"decisions file (default: {REVIEWED} next to the export)")
        if name == "sync":
            c.add_argument("--dry-run", action="store_true", help="only print the changes")
    args = p.parse_args()
    if args.cmd == "export":
        export(args.out)
    elif args.cmd == "summary":
        summary(load(args.file, args.db))
    else:
        reviewed = args.reviewed or os.path.join(os.path.dirname(os.path.abspath(args.file)), REVIEWED)
        if args.cmd == "pending":
            pending(load(args.file), reviewed)
        elif args.cmd == "sync":
            sync(args.file, reviewed, args.dry_run)
        else:
            review(load(args.file), reviewed, args.list, args.all, args.id, args.search_db, args.fixes)


if __name__ == "__main__":
    main()
