#!/usr/bin/env python3
"""Build the web app's search index from the CTC word alignment.

Reads a word alignment ({song: [[word, start, end], ...]}) and writes SQLite:

- words(song, i, w, start, end, line, tok, orig): every sung word in order,
  normalized with web/textnorm.py (no accents/punctuation). The app searches
  phrases of any length on this table. `orig` is the word as written in the
  lyrics file, `tok` its index (one written word can give several normalized
  ones: "to-to-todo"), `line` its line number in the lyrics file the
  alignment came from (text/sung/<song>.txt or text/corpus), so the app can show
  the matching line; NULL when that file no longer matches the alignment.
- Timing corrections from visitors' reports (text/timing_fixes.json, written by
  `tools/reports.py review`) are applied on top of the alignment: see apply_fixes.
- redondos_search: the older FTS5 table of 1..6-word windows, kept so older
  versions of the app keep working.

Usage: build_db.py [alignment.json] [output.db]
       defaults: eval/out/ctc_sung.json → ../web/redondos.db
       FIXES env var: another timing_fixes.json (default text/timing_fixes.json)
"""

import json
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "web"))
sys.path.insert(0, os.path.join(HERE, "eval"))
import textnorm  # noqa: E402  (shared with the app)
from common import FIXES, SUNG, lyrics_path  # noqa: E402

FIXES = os.getenv("FIXES", FIXES)

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "eval", "out", "ctc_sung.json")
DB = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "..", "web", "redondos.db")
MAX_WINDOW = 6


def clean(word):
    """Lowercase, punctuation stripped, accents kept: '"Pop"' -> 'pop', 'dormirá,' -> 'dormirá'.

    Matches how the app compares (exact match on the lowercased query).
    """
    return re.sub(r"[^\w']+", "", word.lower()).strip("'")


def line_numbers(song, words):
    """Line number of each aligned word, from the lyrics file it was aligned with.

    ctc_align.py aligns the whitespace-separated words of sung/<song>.txt (or of
    the corpus file) minus the ones without letters ("—"), in order. Try each
    candidate file and keep the first whose words match the alignment; None if
    none does (the file was edited after aligning: re-run ctc_align.py).
    """
    for path in (os.path.join(SUNG, f"{song}.txt"), lyrics_path(song)):
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            lines = [l.split() for l in f if l.strip()]
        file_words = iter((w, n) for n, line in enumerate(lines) for w in line)
        numbers = []
        for w, _, _ in words:
            for fw, n in file_words:          # skips unaligned file words
                if fw == w:
                    numbers.append(n)
                    break
            else:
                break
        if len(numbers) == len(words):
            return numbers
    return None


def apply_fixes(data, fixes):
    """Apply hand corrections to the alignment; returns {song: [(tok, word, start, end), ...]}.

    Each fix names a run of aligned words by index (first..last, = `tok` in the DB) and
    checks they're still the same words (first_word/last_word), so a fix made before a
    re-alignment or a lyrics edit is skipped with a warning instead of moving the wrong word.
    - "adjust": new start for the first word and/or new end for the last one. Several fixes
      of the same word: the median.
    - "delete": the words are dropped (aligned where nothing is sung). Their `tok` numbers
      stay, so later fixes still point at the right words.
    """
    out = {song: [(tok, w, s, e) for tok, (w, s, e) in enumerate(words)] for song, words in data.items()}
    starts, ends, deleted, applied, skipped = defaultdict(list), defaultdict(list), set(), 0, []
    for f in fixes:
        words = data.get(f["song"])
        ok = (words is not None and 0 <= f["first"] <= f["last"] < len(words)
              and words[f["first"]][0] == f["first_word"] and words[f["last"]][0] == f["last_word"])
        if not ok:
            skipped.append(f"{f['song']} {f['first']}-{f['last']} ({f['report']}): words changed since the fix")
            continue
        applied += 1
        if f["action"] == "delete":
            deleted.update((f["song"], t) for t in range(f["first"], f["last"] + 1))
        else:
            if "start" in f:
                starts[(f["song"], f["first"])].append(f["start"])
            if "end" in f:
                ends[(f["song"], f["last"])].append(f["end"])
    for song, words in out.items():
        fixed = []
        for tok, w, s, e in words:
            if (song, tok) in deleted:
                continue
            s2 = statistics.median(starts[(song, tok)]) if (song, tok) in starts else s
            e2 = statistics.median(ends[(song, tok)]) if (song, tok) in ends else e
            if s2 >= e2:
                skipped.append(f"{song} word {tok} '{w}': corrected times {s2:.3f}-{e2:.3f} are empty, kept {s}-{e}")
                s2, e2 = s, e
            fixed.append((tok, w, round(s2, 3), round(e2, 3)))
        out[song] = fixed
    if fixes:
        print(f"timing fixes: {applied} applied, {len(deleted)} words deleted, {len(skipped)} problems")
        for msg in skipped:
            print(f"   skipped: {msg}")
    return out


def main():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    fixes = []
    if os.path.exists(FIXES):
        with open(FIXES, encoding="utf-8") as f:
            fixes = json.load(f)
    fixed = apply_fixes(data, fixes)
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE words (song TEXT, i INTEGER, w TEXT, start REAL, end REAL,"
                 " line INTEGER, tok INTEGER, orig TEXT, PRIMARY KEY (song, i))")
    n_words, no_lines = 0, []
    for song, words in sorted(data.items()):
        numbers = line_numbers(song, words)
        if numbers is None and words:
            no_lines.append(song)
        i = 0
        for tok, w, s, e in fixed[song]:
            line = numbers[tok] if numbers else None
            # one aligned token can normalize to several words ("to-to-todo"): share its timing
            for part in textnorm.words(w):
                conn.execute("INSERT INTO words VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (song, i, part, s, e, line, tok, w))
                i += 1
        n_words += i
    conn.execute("CREATE INDEX words_w ON words (w)")

    conn.execute("CREATE VIRTUAL TABLE redondos_search USING fts5(song, lyric, start UNINDEXED, end UNINDEXED)")
    rows = 0
    for song, words in sorted(fixed.items()):
        words = [(clean(w), s, e) for _, w, s, e in words if clean(w)]
        for size in range(1, MAX_WINDOW + 1):
            for i in range(len(words) - size + 1):
                win = words[i:i + size]
                conn.execute("INSERT INTO redondos_search VALUES (?, ?, ?, ?)",
                             (song, " ".join(w for w, _, _ in win), win[0][1], win[-1][2]))
                rows += 1
    conn.commit()
    conn.close()
    os.replace(tmp, DB)          # atomic: the app never sees a half-built index
    print(f"{DB}: {len(data)} songs, {n_words} words, {rows} legacy phrases (1-{MAX_WINDOW} words)")
    if no_lines:
        print(f"no line numbers (lyrics file changed since aligning?): {', '.join(no_lines)}")


if __name__ == "__main__":
    main()
