#!/usr/bin/env python3
"""Measure the time offset between the Demucs stems and the original mix.

Cross-correlates (vocals + no_vocals), which should equal the mix, against
music/<song>.mp3. Prints the lag: positive = stems are LATE vs the mix,
negative = stems are EARLY (their timestamps must be shifted by -lag).

Usage: offset.py [<song> ...]   (default: all songs with stems)
"""
import os, sys, warnings
warnings.simplefilter("ignore")
import torch, torchaudio
from common import MUSIC, STEMS

SR = 8000           # plenty for alignment, fast
MAX_LAG_S = 2.0
SPAN_S = 60.0       # correlate a 60 s excerpt from the middle of the song


def load(path):
    """Decode with the ffmpeg CLI (torchcodec rejects some of the original mp3s)."""
    import subprocess, numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return torch.from_numpy(np.frombuffer(raw, dtype=np.float32).copy())


def lag_seconds(song):
    mix = load(os.path.join(MUSIC, f"{song}.mp3"))
    stems = load(os.path.join(STEMS, song, "vocals.mp3"))
    nov = load(os.path.join(STEMS, song, "no_vocals.mp3"))
    n = min(stems.numel(), nov.numel())
    stems = stems[:n] + nov[:n]
    mid, span, L = min(mix.numel(), n) // 2, int(SPAN_S * SR), int(MAX_LAG_S * SR)
    ref = mix[mid - span // 2: mid + span // 2]
    seg = stems[mid - span // 2 - L: mid + span // 2 + L]
    # Normalised cross-correlation over lags -L..L via conv1d
    corr = torch.nn.functional.conv1d(seg.view(1, 1, -1), ref.view(1, 1, -1)).flatten()
    k = int(corr.argmax())
    return (k - L) / SR, float(corr.max() / (ref.norm() * seg.norm() + 1e-9))


if __name__ == "__main__":
    songs = sys.argv[1:] or sorted(s for s in os.listdir(STEMS)
                                   if os.path.exists(os.path.join(MUSIC, f"{s}.mp3")))
    for s in songs:
        lag, c = lag_seconds(s)
        print(f"{s:42} lag {lag:+.3f}s  (corr {c:.2f})", flush=True)
