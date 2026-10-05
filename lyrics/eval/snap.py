#!/usr/bin/env python3
"""Post-process an alignment: snap word boundaries to where the voice is audible.

- Over-long words (> MAX_WORD_S): the aligner stretched the word back over an
  intro or solo, and the real word is at the END of the interval. The start
  moves to the beginning of the last continuous stretch of voice before the end.
- Other words: if the start falls where the vocal stem is silent, it moves
  forward to the first audible moment inside the word.
- Ends: if the end falls in silence, it moves back to the last audible moment.

Usage: snap.py <alignment source> <output.json> [<song> ...]
       (songs default to those with labels in text/labels/)
       e.g. snap.py ../mfa out/mfa_snap.json
"""

import glob
import json
import os
import sys
import warnings

warnings.simplefilter("ignore")

import torch
import torchaudio

from common import HERE, LABELS, STEMS, load_words

HOP_S = 0.01          # 10 ms analysis frames
WIN_S = 0.03          # 30 ms RMS window
BELOW_PEAK_DB = 25    # "audible" = within 25 dB of the song's loud parts
                      # (35 dB let Demucs bleed in intros count as voice)
MAX_WORD_S = 0.8      # longer than this = stretched over a non-sung stretch
MIN_GAP_S = 0.15      # silence needed before a voice run to treat it as a new onset
MIN_RUN_S = 0.08      # ignore audible blips shorter than this (clicks, bleed)


def voice_activity(song):
    """Boolean per 10 ms frame: is the vocal stem audible?"""
    wav, sr = torchaudio.load(os.path.join(STEMS, song, "vocals.mp3"))
    x = wav.mean(0)
    hop, win = int(HOP_S * sr), int(WIN_S * sr)
    frames = x[: (x.numel() - win) // hop * hop + win].unfold(0, win, hop)
    db = 20 * torch.log10(frames.pow(2).mean(1).sqrt() + 1e-9)
    loud = torch.quantile(db, 0.95)           # robust "peak" level
    active = (db > loud - BELOW_PEAK_DB).tolist()
    # Drop short isolated runs of activity
    min_run, i = int(MIN_RUN_S / HOP_S), 0
    while i < len(active):
        if active[i]:
            j = i
            while j < len(active) and active[j]:
                j += 1
            if j - i < min_run:
                active[i:j] = [False] * (j - i)
            i = j
        else:
            i += 1
    return active


def snap(words, active):
    out = []
    n = len(active)
    for w, start, end in words:
        a, b = int(start / HOP_S), min(int(end / HOP_S), n - 1)
        if end - start > MAX_WORD_S and b < n:
            # Last voiced frame at or before the end, then walk back to its run's start
            k = next((i for i in range(b, a - 1, -1) if active[i]), None)
            if k is not None:
                gap, j = int(MIN_GAP_S / HOP_S), k
                while j > a and (active[j - 1] or any(active[max(a, j - gap):j])):
                    j -= 1
                if j > a:
                    start = j * HOP_S
                    a = j
        if a < n and not active[a]:
            k = next((i for i in range(a, b + 1) if active[i]), None)
            if k is not None:
                start = k * HOP_S
        if b < n and not active[b]:
            k = next((i for i in range(b, int(start / HOP_S) - 1, -1) if active[i]), None)
            if k is not None:
                end = (k + 1) * HOP_S
        out.append([w, round(start, 3), round(max(end, start + 0.03), 3)])
    return out


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    source, out_path, songs = sys.argv[1], sys.argv[2], sys.argv[3:]
    if not songs:
        songs = [os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(LABELS, "*.txt"))]
    data = {}
    for song in sorted(songs):
        words = load_words(source, song)
        if not words:
            print(f"{song}: no alignment in {source}, skipped")
            continue
        data[song] = snap(words, voice_activity(song))
        moved = sum(1 for (_, s0, _), (_, s1, _) in zip(words, data[song]) if abs(s0 - s1) > 0.005)
        print(f"{song}: {len(words)} words, {moved} starts moved")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
