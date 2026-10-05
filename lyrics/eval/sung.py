#!/usr/bin/env python3
"""Add the stanza repeats that the lyrics file leaves out (e.g. choruses).

The lyrics file's structure is kept as is: nothing is removed or reordered
(spotting-based reordering proved unreliable). Only additions:

1. Where is each stanza of the file sung? From the full CTC alignment
   (out/ctc.json): first and last word of each stanza.
2. Spot every distinct stanza in the vocal stem (spot.py).
3. A spotted occurrence is ADDED if it is confident (score >= MIN_SCORE) and
   lies in a stretch that no stanza of the file covers. It is inserted
   between the stanzas sung before and after it.

Writes ../sung/<song>.txt (stanzas separated by blank lines) and prints a report.
The original lyrics in ../text/corpus are never modified.

Usage: sung.py <song> [<song> ...] | --all
"""

import os
import sys
import warnings

warnings.simplefilter("ignore")

import json

from common import HERE, LYRICS_DIR, STEMS, SUNG, lyrics_path
from ctc_align import STAR, bundle, romanize
from spot import find_occurrences, line_tokens, read_stanzas, song_emission

CTC = os.path.join(HERE, "out", "ctc.json")
MIN_SCORE = 0.15      # real stanza matches score >= 0.12-0.42, noise <= 0.05; be strict for additions
OVERLAP_S = 1.0       # tolerated overlap with an already-covered stretch


def stanza_spans(stanzas, words):
    """(start, end) of each stanza of the file, from the word alignment (same word order)."""
    spans, k = [], 0
    for st in stanzas:
        n = sum(1 for line in st for w in line.split() if romanize(w))
        ws = words[k:k + n]
        k += n
        spans.append((ws[0][1], ws[-1][2]) if ws else None)
    return spans


def sung_structure(model, dictionary, song, words):
    em, spf = song_emission(model, song)
    stanzas = read_stanzas(song)
    spans = stanza_spans(stanzas, words)
    covered = [sp for sp in spans if sp]
    seq = [(sp[0] if sp else None, i) for i, sp in enumerate(spans)]   # (time, stanza index)
    added, seen = [], set()
    for i, st in enumerate(stanzas):
        ws, tokens = line_tokens(" ".join(st), dictionary)
        key = " ".join(ws)
        if not tokens or key in seen:
            continue
        seen.add(key)
        for s, e, sc in find_occurrences(em, tokens, dictionary[STAR]):
            a, b = s * spf, e * spf
            if sc >= MIN_SCORE and all(b - OVERLAP_S <= x or a + OVERLAP_S >= y for x, y in covered):
                added.append((a, i, sc))
                covered.append((a, b))
    # Insert each addition between the stanzas sung before and after it
    for a, i, sc in sorted(added):
        pos = next((p for p, (t, _) in enumerate(seq) if t is not None and t > a), len(seq))
        seq.insert(pos, (a, i))
    return stanzas, [i for _, i in seq], added


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args == ["--all"]:
        args = sorted(s for s in os.listdir(STEMS)
                      if os.path.exists(os.path.join(STEMS, s, "vocals.mp3")) and lyrics_path(s)
                      and os.path.getsize(lyrics_path(s)) > 0)
    model = bundle.get_model(with_star=True).eval()
    dictionary = bundle.get_dict(star=STAR)
    with open(CTC, encoding="utf-8") as f:
        aligned = json.load(f)
    os.makedirs(SUNG, exist_ok=True)
    for song in args:
        if song not in aligned:
            print(f"{song}: not in {CTC}, run ctc_align.py first")
            continue
        stanzas, order, added = sung_structure(model, dictionary, song, aligned[song])
        with open(os.path.join(SUNG, f"{song}.txt"), "w", encoding="utf-8") as f:
            f.write("\n\n".join("\n".join(stanzas[i]) for i in order) + "\n")
        what = ", ".join(f"stanza {i} '{stanzas[i][0][:20]}' at {a:.1f}s ({sc:.2f})" for a, i, sc in added)
        print(f"{song}: {len(stanzas)} stanzas in file, +{len(added)} added" + (f": {what}" if added else ""), flush=True)


if __name__ == "__main__":
    main()
