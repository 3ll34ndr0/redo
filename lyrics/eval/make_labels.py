#!/usr/bin/env python3
"""Pre-fill Audacity label files so ground truth is quick to mark.

One point label per lyric line, placed where the given alignment thinks the
line starts. You then drag each label to where the line is really sung, add
labels for repeats, and export (see README.md).

Usage: make_labels.py <alignment source> <song> [<song> ...]
       e.g. make_labels.py ../mfa la_bestia_pop etiqueta_negra
Writes ../text/labels_todo/<song>.txt
"""

import os
import sys

from common import CORPUS, HERE, LABELS_TODO, load_words, norm


def line_starts(lines, words):
    """Walk the lyric lines in order, matching each one's first words to the alignment."""
    seq = [norm(w)[0] if norm(w) else "" for w, _, _ in words]
    pos, last_t, out = 0, 0.0, []
    for line in lines:
        key = norm(line)[:3]
        found = None
        for i in range(pos, len(seq) - len(key) + 1):
            if seq[i:i + len(key)] == key:
                found = i
                break
        if found is None:
            t = last_t + 1.0              # not found: put it just after the previous one
        else:
            t, pos = words[found][1], found + len(key)
        out.append((t, line))
        last_t = t
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    source, songs = sys.argv[1], sys.argv[2:]
    os.makedirs(LABELS_TODO, exist_ok=True)
    for song in songs:
        words = load_words(source, song)
        if not words:
            print(f"{song}: no alignment in {source}, skipped")
            continue
        with open(os.path.join(CORPUS, f"{song}.txt"), encoding="utf-8") as f:
            lines = [l.strip() for l in f if norm(l)]
        out = os.path.join(LABELS_TODO, f"{song}.txt")
        with open(out, "w", encoding="utf-8") as f:
            for t, text in line_starts(lines, words):
                f.write(f"{t:.3f}\t{t:.3f}\t{text}\n")
        print(f"{song}: {len(lines)} lines -> {out}")


if __name__ == "__main__":
    main()
