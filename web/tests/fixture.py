"""Made-up test data: two invented songs, never real lyrics (the repo is public).

Same shapes as the real thing: `rows()` gives what the app reads from the words table
(see lyrics/build_db.py), `build_db()` writes such a database, and every word gets a
fake timing (0.4 s per word, 1.5 s between lines, from 5 s on).

    python3 fixture.py <dir>    writes <dir>/redondos.db (audio: see smoke_test.py)
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import textnorm  # noqa: E402

SONGS = {
    "cancion_de_prueba": [
        "Vamos a brillar esta noche",
        "La luna sale sobre el puerto",
        "Vamos a brillar esta noche",
        "Nadie duerme en la ciudad",
        "El pingüino baila, ¡y sueña!",
    ],
    "otra_cancion": [
        "El tren llega tarde otra vez",
        "Brillar no cuesta nada",
        "Mi corazón es un tambor",
        "To-to-todo vuelve a empezar",
    ],
}
SONG_SECONDS = 60        # length of the generated audio per song (all timings fit in it)
WORD_S, GAP_S, START_S = 0.4, 1.5, 5.0


def rows():
    """[(song, w, start, end, line, tok, orig)] ordered by song, then word."""
    out = []
    for song in sorted(SONGS):
        t, tok = START_S, 0
        for line_no, line in enumerate(SONGS[song]):
            for orig in line.split():
                parts = textnorm.words(orig)
                if not parts:
                    continue
                for part in parts:          # "To-to-todo": one written word, three sung ones
                    out.append((song, part, round(t, 3), round(t + WORD_S, 3), line_no, tok, orig))
                tok += 1
                t += WORD_S
            t += GAP_S
    return out


def build_db(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE words (song TEXT, i INTEGER, w TEXT, start REAL, end REAL,"
                 " line INTEGER, tok INTEGER, orig TEXT, PRIMARY KEY (song, i))")
    i, last = 0, None
    for song, w, start, end, line, tok, orig in rows():
        i = 0 if song != last else i + 1
        last = song
        conn.execute("INSERT INTO words VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (song, i, w, start, end, line, tok, orig))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    build_db(os.path.join(out, "redondos.db"))
    print(f"{out}/redondos.db: {len(SONGS)} songs, {len(rows())} words")
