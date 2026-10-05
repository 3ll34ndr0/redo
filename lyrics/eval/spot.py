#!/usr/bin/env python3
"""Find every place each lyric line is sung (line spotting with the CTC model).

For each distinct lyric line: align it alone against the whole song, with a
wildcard on both sides, take the best match, mask that stretch, and repeat.
Each match gets a confidence = mean probability of its characters.

Usage: spot.py <song> [--debug]      prints candidate occurrences and scores
"""

import os
import sys
import warnings

warnings.simplefilter("ignore")

import torch
import torchaudio

from common import HERE, lyrics_path
from ctc_align import STAR, bundle, emissions, load_vocals, romanize

CACHE = os.path.join(HERE, "out", "emissions")
MAX_PER_LINE = 8


def song_emission(model, song):
    """Emission (log-probs per 20 ms frame) for a song, cached on disk."""
    path = os.path.join(CACHE, f"{song}.pt")
    if os.path.exists(path):
        return torch.load(path)
    em, spf = emissions(model, load_vocals(song))
    os.makedirs(CACHE, exist_ok=True)
    torch.save((em, spf), path)
    return em, spf


def read_lines(song):
    with open(lyrics_path(song), encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def line_tokens(line, dictionary):
    words = [romanize(w) for w in line.split()]
    return [w for w in words if w], [[dictionary[c] for c in w] for w in words if w]


def find_occurrences(em, tokens, star_id, blank_id=0):
    """Best non-overlapping matches of one line: [(start_frame, end_frame, score)]."""
    flat = [star_id] + [t for w in tokens for t in w] + [star_id]
    n_chars = len(flat) - 2
    em = em.clone()
    found = []
    for _ in range(MAX_PER_LINE):
        labels, scores = torchaudio.functional.forced_align(
            em.unsqueeze(0), torch.tensor([flat], dtype=torch.int32), blank=blank_id)
        spans = torchaudio.functional.merge_tokens(labels[0], scores[0].exp())
        if len(spans) < n_chars + 2:
            break
        chars = spans[1:1 + n_chars]                 # drop the two wildcard spans
        s, e = chars[0].start, chars[-1].end
        score = sum(c.score for c in chars) / n_chars
        found.append((s, e, score))
        # Mask: in this stretch only blank and wildcard are possible from now on
        keep = em[s:e, [blank_id, star_id]].clone()
        em[s:e] = float("-inf")
        em[s:e, [blank_id, star_id]] = keep
        if score < 0.05:
            break
    return found


def read_stanzas(song):
    """Lyrics as stanzas (blocks separated by blank lines): [[line, ...], ...]."""
    with open(lyrics_path(song), encoding="utf-8") as f:
        blocks = f.read().split("\n\n")
    return [[l.strip() for l in b.splitlines() if l.strip()] for b in blocks if b.strip()]


def main():
    song = sys.argv[1]
    model = bundle.get_model(with_star=True).eval()
    dictionary = bundle.get_dict(star=STAR)
    em, spf = song_emission(model, song)
    seen = set()
    for i, stanza in enumerate(read_stanzas(song)):
        words, tokens = line_tokens(" ".join(stanza), dictionary)
        key = " ".join(words)
        if not tokens or key in seen:
            continue
        seen.add(key)
        occ = find_occurrences(em, tokens, dictionary[STAR])
        print(f"#{i} {stanza[0][:30]:30}…  " + "  ".join(f"{s * spf:6.1f}-{e * spf:5.1f}s({sc:.2f})" for s, e, sc in sorted(occ)))


if __name__ == "__main__":
    main()
