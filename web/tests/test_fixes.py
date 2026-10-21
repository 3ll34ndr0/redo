"""Timing corrections (web/fixes.py) on made-up words."""

import pytest

import fixes


def song(*spans):
    """{tok: (word, start, end)}: words w0, w1, ... with the given times."""
    return {tok: (f"w{tok}", s, e) for tok, (s, e) in enumerate(spans)}


def fix(first, last, action="adjust", **times):
    return dict(report=f"{first}-{last}", song="s", action=action, first=first, last=last,
                first_word=f"w{first}", last_word=f"w{last}", **times)


def test_fit_span_moves_the_edges():
    times = {0: [1.0, 1.5], 1: [1.5, 2.0], 2: [2.0, 2.5]}
    assert fixes.fit_span(times, [0, 1, 2], 0.8, 2.7)
    assert times == {0: [0.8, 1.5], 1: [1.5, 2.0], 2: [2.0, 2.7]}


def test_fit_span_spreads_the_words_left_outside():
    # the last two words were placed after the real end: spread between the last word inside and the end
    times = {0: [1.0, 1.5], 1: [9.0, 9.5], 2: [9.5, 10.0]}
    assert fixes.fit_span(times, [0, 1, 2], None, 2.5)
    assert times[0] == [1.0, 1.5]
    assert times[1] == pytest.approx([1.5, 2.0]) and times[2] == pytest.approx([2.0, 2.5])


def test_fit_span_refuses_an_empty_span():
    times = {0: [1.0, 2.0]}
    assert not fixes.fit_span(times, [0], 2.0, 2.0)
    assert times == {0: [1.0, 2.0]}


def test_adjust_takes_the_median_of_several_fixes():
    words = {"s": song((1, 2), (2, 3), (3, 4))}
    times, deleted, applied, problems = fixes.apply(words, [fix(0, 2, start=0.5), fix(0, 2, start=0.7),
                                                           fix(0, 2, start=0.9, end=4.4)])
    assert times["s"][0][0] == 0.7 and times["s"][2][1] == 4.4
    assert len(applied) == 3 and not deleted and not problems


def test_delete():
    times, deleted, applied, problems = fixes.apply({"s": song((1, 2), (2, 3), (3, 4))}, [fix(1, 2, "delete")])
    assert deleted == {("s", 1), ("s", 2)} and not problems


def test_changed_words_are_skipped():
    words = {"s": song((1, 2), (2, 3))}
    stale = dict(fix(0, 1, start=0.5), last_word="other")
    times, _, applied, problems = fixes.apply(words, [stale, fix(0, 5, start=0.5), dict(fix(0, 1), song="x")])
    assert times["s"] == {0: [1, 2], 1: [2, 3]} and applied == [] and len(problems) == 3


def test_gaps_in_toks():
    # tok 1 was deleted by an earlier build: the fix uses the words still there
    words = {"s": {0: ("w0", 1, 2), 2: ("w2", 3, 4)}}
    times, _, applied, problems = fixes.apply(words, [fix(0, 2, end=4.5)])
    assert times["s"] == {0: [1, 2], 2: [3, 4.5]} and len(applied) == 1 and not problems


def test_from_report_keeps_only_the_moved_edges():
    r = dict(id=7, created_at="2026-10-20T10:00:00Z", song="s", first_tok=0, last_tok=2, first_word="w0",
             last_word="w2", start_ms=1000, end_ms=4000, fixed_start_ms=800, fixed_end_ms=4000)
    f = fixes.from_report(r)
    assert f["report"] == "7@2026-10-20T10:00:00Z" and f["action"] == "adjust"
    assert f["start"] == 0.8 and "end" not in f


def test_from_report_prefers_the_admins_times():
    r = dict(id=1, created_at="2026-10-21T10:00:00Z", song="s", start_ms=1000, end_ms=2000, first_tok=3, last_tok=4,
             first_word="a", last_word="b", fixed_start_ms=900, fixed_end_ms=2000, status="accepted",
             review_start_ms=950, review_end_ms=2100)
    fix = fixes.from_report(r)
    assert (fix["start"], fix["end"]) == (0.95, 2.1)
    assert "end" not in fixes.from_report(dict(r, review_start_ms=None, review_end_ms=None))
