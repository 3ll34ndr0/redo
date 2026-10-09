"""Shared helpers for the alignment evaluation scripts."""

import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
LYRICS_DIR = os.path.join(HERE, "..")
# Lyrics (copyrighted) live in text/, a separate PRIVATE git repo (redo-letras)
TEXT = os.path.join(LYRICS_DIR, "text")
CORPUS = os.path.join(TEXT, "corpus")                # <name>.txt lyrics as scraped
SUNG = os.path.join(TEXT, "sung")                    # <song>.txt lyrics as sung (sung.py + hand edits)
LABELS = os.path.join(TEXT, "labels")                # ground truth: Audacity labels per song
LABELS_TODO = os.path.join(TEXT, "labels_todo")      # pre-filled labels to correct (make_labels.py)
FIXES = os.path.join(TEXT, "timing_fixes.json")      # hand corrections from clip reports (build_db.py)
MUSIC = os.path.join(LYRICS_DIR, "music")            # <song>.mp3 original mix
STEMS = os.path.join(LYRICS_DIR, "separated", "htdemucs_ft")


def norm(text):
    """Lowercase, strip accents and punctuation: 'Tú, ¡y él!' -> ['tu', 'y', 'el']."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)


# Stem/music names whose lyrics file has a different name that the
# accent/punctuation-insensitive match below can't find.
LYRICS_ALIASES = {
    "la_murga_de_la_virgencita": "murga_de_la_virgencita",
    "roto_molhado": "rato_molhado",
}


def _name_key(name):
    """'mariposa_pontiac_-_rock_del_país' and 'mariposa_pontiac_rock_del_pais' -> same key."""
    name = unicodedata.normalize("NFD", name.lower())
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", name)


def lyrics_path(song):
    """Lyrics file for a song (named after its stem/music file), or None.

    Tries the exact name, then an alias, then a match ignoring accents and punctuation.
    """
    for name in (song, LYRICS_ALIASES.get(song)):
        if name and os.path.exists(os.path.join(CORPUS, f"{name}.txt")):
            return os.path.join(CORPUS, f"{name}.txt")
    want = _name_key(LYRICS_ALIASES.get(song, song))
    for f in os.listdir(CORPUS):
        if f.endswith(".txt") and _name_key(os.path.splitext(f)[0]) == want:
            return os.path.join(CORPUS, f)
    return None


def load_words(source, song):
    """Return [(word, start, end), ...] for a song from an alignment source.

    source is either a directory of MFA TextGrids (<song>.TextGrid, tier 'words')
    or a JSON file {song: [[word, start, end], ...]} written by other aligners.
    """
    if os.path.isdir(source):
        from praatio import textgrid
        path = os.path.join(source, f"{song}.TextGrid")
        if not os.path.exists(path):
            return None
        tg = textgrid.openTextgrid(path, False)
        tier = tg.getTier("words" if "words" in tg.tierNames else tg.tierNames[0])
        return [(e.label, e.start, e.end) for e in tier.entries if e.label.strip()]
    with open(source, encoding="utf-8") as f:
        data = json.load(f)
    return [tuple(w) for w in data[song]] if song in data else None


def read_labels(path):
    """Audacity label export: 'start<TAB>end<TAB>text' per line -> [(start, end, text)].

    end is None for point labels (start == end): the line's end wasn't marked.
    """
    labels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3 and parts[0].strip() and not parts[0].startswith("\\"):
                start, end = float(parts[0]), float(parts[1])
                labels.append((start, end if end - start > 0.05 else None, parts[2]))
    return labels
