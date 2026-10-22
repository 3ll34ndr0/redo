"""Timing corrections on top of the word alignment, shared by lyrics/build_db.py and the app.

A fix names a run of aligned words by index (first..last, = `tok` in the search database)
and checks they're still the same words (first_word/last_word), so a fix made before a
re-alignment or a lyrics edit is skipped instead of moving the wrong words.
- "delete": the words are dropped (aligned where nothing is sung).
- "adjust": the phrase first..last gets a new start and/or end (fit_span). Several fixes
  of the same phrase: the median.

Fixes come from visitors' reports (¿No coincide?): reviewed ones from text/timing_fixes.json
(baked into the database by build_db.py), and provisional ones from the reports database,
applied by the app in memory until they're reviewed (see from_report).
"""

import statistics
from collections import defaultdict

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


def apply(words, fixes):
    """Apply fixes to aligned words {song: {tok: (word, start, end)}}.

    Returns (times {song: {tok: [start, end]}}, deleted {(song, tok)}, applied [fix], problems [str]).
    Toks may have gaps (words deleted by an earlier build): a fix's range uses the ones there.
    """
    times = {song: {tok: [s, e] for tok, (_, s, e) in ws.items()} for song, ws in words.items()}
    deleted, spans, applied, problems = set(), defaultdict(lambda: ([], [])), [], []
    for f in fixes:
        ws = words.get(f["song"])
        ok = (ws is not None and f["first"] <= f["last"] and f["first"] in ws and f["last"] in ws
              and ws[f["first"]][0] == f["first_word"] and ws[f["last"]][0] == f["last_word"])
        if not ok:
            problems.append(f"{f['song']} {f['first']}-{f['last']} ({f['report']}): words changed since the fix")
            continue
        applied.append(f)
        if f["action"] == "delete":
            deleted.update((f["song"], t) for t in range(f["first"], f["last"] + 1))
        else:
            starts, ends = spans[(f["song"], f["first"], f["last"])]
            if "start" in f:
                starts.append(f["start"])
            if "end" in f:
                ends.append(f["end"])
    for (song, first, last), (starts, ends) in sorted(spans.items()):
        toks = [t for t in range(first, last + 1) if t in times[song] and (song, t) not in deleted]
        start = statistics.median(starts) if starts else None
        end = statistics.median(ends) if ends else None
        if toks and not fit_span(times[song], toks, start, end):
            problems.append(f"{song} {first}-{last}: corrected span {start}-{end} is empty, not applied")
    return times, deleted, applied, problems


def report_key(r):
    """A report's id in timing_fixes.json and the database's fixes table. Ids restart if the
    server's reports database is ever lost: the creation time keeps keys unique."""
    return f"{r['id']}@{r['created_at']}"


def adjusted_ms(r):
    """A report's adjustment (start_ms, end_ms): the admin's times if set on review (/admin), else
    the visitor's; (None, None) if neither."""
    if r.get("review_start_ms") is not None:
        return r["review_start_ms"], r["review_end_ms"]
    return r.get("fixed_start_ms"), r.get("fixed_end_ms")


def from_report(r):
    """The fix a report asks for (a row of the reports database with its words: first_tok..last_tok),
    as in timing_fixes.json: "delete" once reviewed so (status deleted), else "adjust" with only
    the edges moved (adjusted_ms: the admin's, else the visitor's)."""
    fix = {"report": report_key(r), "song": r["song"], "action": "adjust", "first": r["first_tok"],
           "last": r["last_tok"], "first_word": r["first_word"], "last_word": r["last_word"]}
    if r.get("status") == "deleted":
        return dict(fix, action="delete")
    start, end = adjusted_ms(r)
    if start != r["start_ms"]:
        fix["start"] = start / 1000
    if end != r["end_ms"]:
        fix["end"] = end / 1000
    return fix
