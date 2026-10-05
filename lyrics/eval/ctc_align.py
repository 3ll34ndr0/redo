#!/usr/bin/env python3
"""CTC forced alignment of lyrics on the Demucs vocal stem (torchaudio MMS_FA).

Differences from the MFA run:
- A wildcard token "*" sits between lyric lines (and at both ends). It can
  absorb any audio (intro, solos, breaths, ad-libs, bleed), so silence is not
  glued onto the first word of the next line.
- The MMS model is trained on 1,100+ languages of varied speech, and CTC
  alignment tends to place onsets where the sound actually is.

Usage: ctc_align.py <song> [<song> ...]        (writes out/ctc.json, merged)
       ctc_align.py --all                     (every song with lyrics + stem)
       ctc_align.py --missing                 (only songs not yet in out/ctc.json)
       add --sung to use ../sung/<song>.txt (from sung.py) when it exists,
       writing out/ctc_sung.json instead

Songs are named after their stem (= music/<song>.mp3, what the web app cuts);
the lyrics file is found by common.lyrics_path (accents/punctuation-insensitive).
"""

import json
import os
import sys
import time
import unicodedata
import warnings

warnings.simplefilter("ignore")

import torch
import torchaudio
from torchaudio.pipelines import MMS_FA as bundle

from common import HERE, STEMS, SUNG, lyrics_path

OUT = os.path.join(HERE, "out", "ctc.json")
SUNG_DIR = SUNG
USE_SUNG = False
CHUNK_S, CONTEXT_S = 30.0, 1.0      # emissions computed in 30 s chunks with 1 s context
SILENCE_GUARD = True                # no letters where the vocal stem is silent
GUARD_MARGIN_S = 0.15               # keep letters possible this close to audible voice
STRANDED_GAP_S = 0.3                # silence inside a line that a short word can't be stranded behind
STRANDED_MAX_LETTERS = 2            # only tiny words ("a", "y", "mi") get stranded; longer ones are real pauses
STRANDED_MAX_DUR_S = 0.1
STAR = "*"


def romanize(word):
    """MMS_FA vocabulary is lowercase a-z plus apostrophe: 'Führer' -> 'fuhrer', 'sueños' -> 'suenos'."""
    w = unicodedata.normalize("NFD", word.lower())
    w = "".join(c for c in w if unicodedata.category(c) != "Mn")
    return "".join(c for c in w if c.isalpha() and c.isascii() or c == "'")


def load_vocals(song):
    wav, sr = torchaudio.load(os.path.join(STEMS, song, "vocals.mp3"))
    wav = wav.mean(0, keepdim=True)
    return torchaudio.functional.resample(wav, sr, bundle.sample_rate)


def emissions(model, wav):
    """Log-probabilities per frame for the whole song, computed chunk by chunk."""
    sr, n = bundle.sample_rate, wav.size(1)
    chunk, ctx = int(CHUNK_S * sr), int(CONTEXT_S * sr)
    parts, frames_per_sample = [], None
    with torch.inference_mode():
        for start in range(0, n, chunk):
            a, b = max(0, start - ctx), min(n, start + chunk + ctx)
            em, _ = model(wav[:, a:b])
            em = em[0]
            frames_per_sample = em.size(0) / (b - a)
            lo = round((start - a) * frames_per_sample)
            hi = lo + round((min(start + chunk, n) - start) * frames_per_sample)
            parts.append(em[lo:hi])
    em = torch.log_softmax(torch.cat(parts), dim=-1)
    return em, n / em.size(0) / sr          # emission, seconds per frame


def guard_silence(em, sec_per_frame, song, dictionary):
    """Forbid letters in frames where the vocal stem is silent (only blank/wildcard allowed).

    Without this, lines the model can't recognise (group vocals, heavy effects)
    may be placed in a silent instrumental stretch just as well as where they're sung.
    """
    from snap import HOP_S, voice_activity
    act = torch.tensor(voice_activity(song), dtype=torch.float32)
    # Dilate by the margin, then resample from 10 ms hops to emission frames
    m = int(GUARD_MARGIN_S / HOP_S)
    act = torch.nn.functional.max_pool1d(act.view(1, 1, -1), 2 * m + 1, 1, m).flatten()
    idx = (torch.arange(em.size(0)) * sec_per_frame / HOP_S).long().clamp(max=act.numel() - 1)
    silent = act[idx] == 0
    if silent.all():
        return em
    em = em.clone()
    keep = [0, dictionary[STAR]]                 # blank, wildcard
    saved = em[silent][:, keep]
    em[silent] = -1e4
    em[silent.nonzero().flatten().unsqueeze(1), torch.tensor(keep)] = saved
    return em


def align_song(model, dictionary, song):
    sung = os.path.join(SUNG_DIR, f"{song}.txt")
    with open(sung if USE_SUNG and os.path.exists(sung) else lyrics_path(song), encoding="utf-8") as f:
        lines = [l.split() for l in f if l.strip()]
    # Transcript with a wildcard between lines; remember which entries are real words
    transcript, originals, line_of = [STAR], [None], [None]
    for n, line in enumerate(lines):
        for w in line:
            r = romanize(w)
            if r:
                transcript.append(r)
                originals.append(w)
                line_of.append(n)
        transcript.append(STAR)
        originals.append(None)
        line_of.append(None)

    cache = os.path.join(HERE, "out", "emissions", f"{song}.pt")     # written by spot.py
    if os.path.exists(cache):
        em, sec_per_frame = torch.load(cache)
    else:
        em, sec_per_frame = emissions(model, load_vocals(song))
    if SILENCE_GUARD:
        em = guard_silence(em, sec_per_frame, song, dictionary)
    tokens = [[dictionary[c] for c in w] for w in transcript]
    flat = torch.tensor([[t for w in tokens for t in w]], dtype=torch.int32)
    labels, scores = torchaudio.functional.forced_align(em.unsqueeze(0), flat, blank=0)
    spans = torchaudio.functional.merge_tokens(labels[0], scores[0].exp())

    # Group token spans back into words
    words, lines_idx, k = [], [], 0
    for w, orig, ln in zip(tokens, originals, line_of):
        ws = spans[k:k + len(w)]
        k += len(w)
        if orig is not None:
            words.append([orig, round(ws[0].start * sec_per_frame, 3), round(ws[-1].end * sec_per_frame, 3)])
            lines_idx.append(ln)
    return unstrand(words, lines_idx, song)


def unstrand(words, lines_idx, song):
    """Move words stranded before a silent gap to where their line resumes.

    E.g. "A | brillar, mi amor": CTC glued the one-letter "A" to the held end of
    the previous line, 2.5 s before "brillar". Inside one lyric line the voice
    doesn't stop for longer than STRANDED_GAP_S right after a tiny word, so a
    word of <= STRANDED_MAX_LETTERS letters lasting <= STRANDED_MAX_DUR_S,
    followed by such a silence in the same line, moves to just before the next
    word. (Singers do pause inside written lines, so longer words stay put.)
    """
    from snap import HOP_S, voice_activity
    act = voice_activity(song)
    gap = int(STRANDED_GAP_S / HOP_S)

    def silent_between(a, b):
        i, j = int(a / HOP_S), min(int(b / HOP_S), len(act))
        run = 0
        for x in act[i:j]:
            run = 0 if x else run + 1
            if run >= gap:
                return True
        return False

    for i in range(len(words) - 2, -1, -1):          # backwards: fixes cascade to earlier words
        if lines_idx[i] != lines_idx[i + 1]:
            continue
        w, nxt = words[i], words[i + 1]
        tiny = len(romanize(w[0])) <= STRANDED_MAX_LETTERS and w[2] - w[1] <= STRANDED_MAX_DUR_S
        if tiny and silent_between(w[2], nxt[1]):
            # Onset of the voice run that contains the next word's start
            j = min(int(nxt[1] / HOP_S), len(act) - 1)
            while j > 0 and act[j - 1]:
                j -= 1
            onset = min(j * HOP_S, nxt[1])
            dur = min(w[2] - w[1], max(0.03, nxt[1] - onset))
            w[1], w[2] = round(max(onset, nxt[1] - dur), 3), round(nxt[1], 3)
    return words


def main():
    global OUT, USE_SUNG
    args = sys.argv[1:]
    if "--sung" in args:
        args.remove("--sung")
        USE_SUNG, OUT = True, os.path.join(HERE, "out", "ctc_sung.json")
    if not args:
        sys.exit(__doc__)
    if args in (["--all"], ["--missing"]):
        done = set(json.load(open(OUT, encoding="utf-8"))) if os.path.exists(OUT) and args == ["--missing"] else set()
        args = sorted(s for s in os.listdir(STEMS)
                      if os.path.exists(os.path.join(STEMS, s, "vocals.mp3")) and lyrics_path(s) and s not in done)
        print(f"{len(args)} songs to align", flush=True)
    model = bundle.get_model(with_star=True)
    model.eval()
    dictionary = bundle.get_dict(star=STAR)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    data = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    for song in args:
        t = time.time()
        data[song] = align_song(model, dictionary, song)
        print(f"{song}: {len(data[song])} words in {time.time() - t:.0f}s", flush=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
