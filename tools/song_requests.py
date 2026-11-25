#!/usr/bin/env python3
"""Songs visitors asked for on the site (web/song_requests.py), most asked first.

    tools/song_requests.py list [--all]              the open ones (--all: closed too)
    tools/song_requests.py close <id>... --as published|rejected

Same server and namespace variables as tools/reports.py (staging: EXTRACTOS_NAMESPACE=unaudio-dev).
"""

import argparse
import json
import sys

from reports import in_pod

DUMP = """import json, sqlite3
c = sqlite3.connect("file:/reports/reports.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print(json.dumps([dict(r) for r in c.execute("SELECT * FROM song_requests ORDER BY times DESC, id")]))
"""
CLOSE = """import json, sqlite3, time
ids, status = json.loads(%r)
now = time.strftime("%%Y-%%m-%%dT%%H:%%M:%%SZ", time.gmtime())
c = sqlite3.connect("/reports/reports.db", timeout=10)
n = sum(c.execute("UPDATE song_requests SET status = ?, closed_at = ? WHERE id = ? AND status = 'open'",
                  (status, now, i)).rowcount for i in ids)
c.commit()
print(n)
"""


def run(script):
    r = in_pod("python -", stdin=script)
    if r.returncode != 0:
        sys.exit(f"failed: {r.stderr.strip()}")
    return r.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("close")
    p.add_argument("ids", nargs="+", type=int)
    p.add_argument("--as", dest="status", required=True, choices=("published", "rejected"))
    args = ap.parse_args()
    if args.cmd == "list":
        rows = [r for r in json.loads(run(DUMP)) if args.all or r["status"] == "open"]
        for r in rows:
            print(f"#{r['id']}  {r['times']}x  {r['title']} — {r['artist'] or '(no artist)'}  [{r['status']}]"
                  f"  first {r['created_at'][:10]}, last {r['last_at'][:10]}  searched: {r['query'] or '-'}")
        print(f"{len(rows)} requests")
    else:
        n = int(run(CLOSE % json.dumps([args.ids, args.status])))
        print(f"{n} of {len(args.ids)} closed as {args.status}")


if __name__ == "__main__":
    main()
