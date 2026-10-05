# Alignment evaluation

Measures how close an aligner's word timings are to reality, so changes to the
pipeline (MFA settings, CTC alignment, Whisper…) can be compared with numbers.

```
eval/
├── make_labels.py           creates ../text/labels_todo/ from an alignment
├── score.py                 scores alignments against ../text/labels/
└── common.py
text/                        PRIVATE lyrics repo (labels contain lyric lines)
├── labels_todo/<song>.txt   pre-filled Audacity labels (one per lyric line, at MFA's guess)
└── labels/<song>.txt        ground truth: the same labels, corrected by hand
```

Run with the project venv: `../../venv/bin/python score.py ../mfa`

## Test songs

| Song | Why |
|------|-----|
| `la_bestia_pop` | MFA looks good (5% over-long words) |
| `divina_tv_führer` | MFA looks good (3%) |
| `etiqueta_negra` | MFA looks bad (19%, a 15.7 s "word") |
| `la_murga_de_los_renegados` | MFA looks bad (23%) |

## Marking the ground truth in Audacity (≈ 10 min per song)

1. `audacity audio/<song>.flac`. It's a stereo file made with ffmpeg: **left = the original
   mix, right = Demucs vocals only**, on exactly the timeline the aligners and the
   web app's cutter use. (Opening the mp3s separately in Audacity can make the
   vocals look ~0.37 s "early": the line's first consonant is visible in the vocals
   but buried in the mix. It's not a real offset.) Split the channels into two tracks
   (track menu → Split Stereo Track) to see the vocals waveform larger.
2. **File → Import → Labels…** → `../text/labels_todo/<song>.txt`. Each label is a lyric
   line, placed where MFA thinks it starts.
3. For each label, play a bit before it, then **drag the label to the first rise of
   the vocals waveform** (right channel): the first consonant, not the vowel. Zoom in (Ctrl+1) so a couple of seconds
   fill the screen. Aim for about 0.1 s precision.
   - ⚠️ The pre-filled position is MFA's guess. Don't keep it because it's "close
     enough"; that would make MFA look better than it is.
4. **Repeated lines (choruses):** a line sung several times needs one label each
   time. Put the cursor at the start, press **Ctrl+B**, and type (or paste) the
   same text. The scorer only needs the first 3 words to match.
   - If you label a line, label **every** time it's sung, or the scorer will
     count a correct prediction at an unlabelled repeat as an error.
5. Lines that aren't sung in this recording: delete their label.
   Lines you can't place confidently: delete them too. Fewer good labels
   beat many doubtful ones.
6. **File → Export → Export Labels…** → `../text/labels/<song>.txt` (same file name).

Then:

```bash
../../venv/bin/python score.py ../mfa
```

## Metrics

For each labelled line start, the scorer takes the aligner's closest occurrence
of the same phrase (its first 3 words) and measures the time difference: what a
user gets when searching that phrase.

- **≤0.3 s / ≤1 s**: share of labelled lines whose start is predicted that close.
  0.3 s is roughly "the clip starts on the right syllable".
- **median**: typical error.
- **missing**: labelled lines the aligner never placed (e.g. repeats that
  aren't in the lyrics file).
