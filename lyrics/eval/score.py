#!/usr/bin/env python3
"""Score an alignment against hand-marked line starts.

Usage: score.py <alignment source> [<alignment source> ...]
       e.g. score.py ../mfa                      # MFA TextGrids
            score.py ../mfa ctc_words.json       # compare two aligners

Ground truth: ../text/labels/<song>.txt (Audacity label export). Each label marks the
moment a lyric line starts being sung; its text is the line (or at least its
first words). If a line is sung several times, it has one label per time.

A phrase = the first 3 words of a label. For every labelled occurrence we take
the closest predicted occurrence of the same phrase and measure the start error.
That is what a user gets: search a phrase, get a clip starting at the predicted time.

  within 0.3s / 1s  share of labelled phrases whose closest prediction is that close
  median err        median |error| over labelled phrases that were found
  missing           labelled phrases the aligner never placed (e.g. unaligned repeats)
  clips ≤0.3s/≤1s   the other direction: for every occurrence the aligner PREDICTS for a
                    labelled line, distance to the nearest real occurrence. "Are the clips
                    the app returns correct?" Unaffected by repeats missing from the lyrics.
  end ≤0.3s / ≤1s   same for line ends, on labels whose end was marked (dragged right
                    edge); predicted end = end of the line's last word in that occurrence
"""

import glob
import os
import statistics
import sys

from common import HERE, LABELS, load_words, norm, read_labels

PHRASE = 3


def occurrences(words, key):
    """Indices where the phrase key starts in the aligned word list."""
    seq = [norm(w)[0] if norm(w) else "" for w, _, _ in words]
    return [i for i in range(len(seq) - len(key) + 1) if seq[i:i + len(key)] == key]


def score(source, label_files):
    per_song, all_err, all_missing, all_n, all_end, all_clip = [], [], 0, 0, [], []
    for path in label_files:
        song = os.path.splitext(os.path.basename(path))[0]
        words = load_words(source, song)
        if words is None:
            per_song.append((song, None))
            continue
        errs, missing, end_errs = [], 0, []
        labels = read_labels(path)
        # Clip accuracy: predicted occurrences of each labelled phrase vs its true occurrences
        truth = {}
        for t, _, text in labels:
            if norm(text)[:PHRASE]:
                truth.setdefault(tuple(norm(text)[:PHRASE]), []).append(t)
        # Ignore matches inside another labelled line ("a brillar mi" inside "vamos a brillar mi amor")
        inside = set()
        for _, _, text in labels:
            line = norm(text)
            for j in occurrences(words, line):
                inside.update(range(j + 1, j + len(line)))
        clip_errs = [min(abs(words[i][1] - t) for t in ts)
                     for key, ts in truth.items() for i in occurrences(words, list(key)) if i not in inside]
        for t, t_end, text in labels:
            line = norm(text)
            key = line[:PHRASE]
            if not key:
                continue
            preds = occurrences(words, key)
            if not preds:
                missing += 1
                continue
            best = min(preds, key=lambda i: abs(words[i][1] - t))
            errs.append(abs(words[best][1] - t))
            last = best + len(line) - 1
            if t_end is not None and last < len(words):
                end_errs.append(abs(words[last][2] - t_end))
        n = len(errs) + missing
        per_song.append((song, (n, errs, missing, end_errs, clip_errs)))
        all_err += errs
        all_missing += missing
        all_n += n
        all_end += end_errs
        all_clip += clip_errs
    return per_song, (all_n, all_err, all_missing, all_end, all_clip)


def fmt(stats):
    n, errs, missing, end_errs, clip_errs = stats
    if not n:
        return "no labels"
    w03 = sum(e <= 0.3 for e in errs) / n
    w1 = sum(e <= 1.0 for e in errs) / n
    med = statistics.median(errs) if errs else float("nan")
    out = f"start: n={n:3}  ≤0.3s {w03:4.0%}  ≤1s {w1:4.0%}  median {med:6.2f}s  missing {missing}"
    if clip_errs:
        c03 = sum(e <= 0.3 for e in clip_errs) / len(clip_errs)
        c1 = sum(e <= 1.0 for e in clip_errs) / len(clip_errs)
        out += f"  |  clips: n={len(clip_errs):3}  ≤0.3s {c03:4.0%}  ≤1s {c1:4.0%}"
    if end_errs:
        e03 = sum(e <= 0.3 for e in end_errs) / len(end_errs)
        e1 = sum(e <= 1.0 for e in end_errs) / len(end_errs)
        out += f"  |  end: n={len(end_errs)}  ≤0.3s {e03:4.0%}  ≤1s {e1:4.0%}  median {statistics.median(end_errs):5.2f}s"
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    label_files = sorted(glob.glob(os.path.join(LABELS, "*.txt")))
    if not label_files:
        sys.exit("No ground truth yet: put Audacity label exports in text/labels/ (see README.md)")
    for source in sys.argv[1:]:
        per_song, total = score(source, label_files)
        print(f"\n== {source}")
        for song, stats in per_song:
            print(f"  {song:34} {fmt(stats) if stats else 'no alignment for this song'}")
        print(f"  {'TOTAL':34} {fmt(total)}")


if __name__ == "__main__":
    main()
