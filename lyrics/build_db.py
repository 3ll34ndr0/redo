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


MIN_WORD_S = 0.02     # shortest word a fix can produce (a clip needs start < end)


def spread(times, toks, a, b):
    """Place words evenly, in order, between a and b."""
    if b - a < MIN_WORD_S * len(toks):
        a = b - MIN_WORD_S * len(toks) if toks else a
    d = (b - a) / len(toks)
    for k, t in enumerate(toks):
        times[t] = [a + k * d, a + (k + 1) * d]


def fit_span(times, toks, start, end):
    """Make a phrase (words toks, in order) start at `start` and end at `end`.

    Words sung inside the new span keep their times (cut to the span); words left
    outside it (placed before the start or after the end) are spread evenly in the gap
    between the span's edge and the nearest word inside. E.g. nine "sí" of which the
    aligner squeezed three before the real start and put three over music 8 s later.
    """
    start = times[toks[0]][0] if start is None else start
    end = times[toks[-1]][1] if end is None else end
    if start >= end:
        return False
    inside = [t for t in toks if times[t][1] > start and times[t][0] < end]
    if not inside:
        spread(times, toks, start, end)
        return True
    for t in inside:
        times[t] = [max(times[t][0], start), min(times[t][1], end)]
    before = [t for t in toks if t < inside[0]]
    after = [t for t in toks if t > inside[-1]]
    if before:
        spread(times, before, start, times[inside[0]][0])
    else:
        times[inside[0]][0] = start
    if after:
        spread(times, after, times[inside[-1]][1], end)
    else:
        times[inside[-1]][1] = end
    return True


def apply_fixes(data, fixes):
    """Apply hand corrections to the alignment; returns {song: [(tok, word, start, end), ...]}.

    Each fix names a run of aligned words by index (first..last, = `tok` in the DB) and
    checks they're still the same words (first_word/last_word), so a fix made before a
    re-alignment or a lyrics edit is skipped with a warning instead of moving the wrong word.
    - "delete": the words are dropped (aligned where nothing is sung). Their `tok` numbers
      stay, so later fixes still point at the right words.
    - "adjust": the phrase first..last gets a new start and/or end (fit_span). Several fixes
      of the same phrase: the median.
    """
    times = {song: {tok: [s, e] for tok, (_, s, e) in enumerate(words)} for song, words in data.items()}
    deleted, spans, applied, problems = set(), defaultdict(lambda: ([], [])), 0, []
    for f in fixes:
        words = data.get(f["song"])
        ok = (words is not None and 0 <= f["first"] <= f["last"] < len(words)
              and words[f["first"]][0] == f["first_word"] and words[f["last"]][0] == f["last_word"])
        if not ok:
            problems.append(f"{f['song']} {f['first']}-{f['last']} ({f['report']}): words changed since the fix")
            continue
        applied += 1
        if f["action"] == "delete":
            deleted.update((f["song"], t) for t in range(f["first"], f["last"] + 1))
        else:
            starts, ends = spans[(f["song"], f["first"], f["last"])]
            if "start" in f:
                starts.append(f["start"])
            if "end" in f:
                ends.append(f["end"])
    for (song, first, last), (starts, ends) in sorted(spans.items()):
        toks = [t for t in range(first, last + 1) if (song, t) not in deleted]
        start = statistics.median(starts) if starts else None
        end = statistics.median(ends) if ends else None
        if toks and not fit_span(times[song], toks, start, end):
            problems.append(f"{song} {first}-{last}: corrected span {start}-{end} is empty, not applied")
    out = {song: [(tok, w, round(times[song][tok][0], 3), round(times[song][tok][1], 3))
                  for tok, (w, _, _) in enumerate(words) if (song, tok) not in deleted]
           for song, words in data.items()}
    if fixes:
        print(f"timing fixes: {applied} applied, {len(deleted)} words deleted, {len(problems)} problems")
        for msg in problems:
            print(f"   problem: {msg}")
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
