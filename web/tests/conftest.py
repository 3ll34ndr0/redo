"""Puts web/ and this folder on the import path, and points the app at made-up data
(fixture.py) before it's imported: database, one audio file per song, a clip cache."""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..")]

import fixture  # noqa: E402

DATA = tempfile.mkdtemp(prefix="extractos-test-")
HAS_FFMPEG = shutil.which("ffmpeg") is not None

fixture.build_db(os.path.join(DATA, "redondos.db"))
os.makedirs(os.path.join(DATA, "music"))
for song in fixture.SONGS:
    path = os.path.join(DATA, "music", f"{song}.mp3")
    if HAS_FFMPEG:      # a real mp3 (a tone), so clips can be cut
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={fixture.SONG_SECONDS}",
                        "-c:a", "libmp3lame", "-b:a", "64k", path], check=True)
    else:               # enough for search (it only checks the file exists)
        open(path, "wb").close()

os.environ.update(DB_PATH=os.path.join(DATA, "redondos.db"), LIBRARY_PATH=os.path.join(DATA, "music"),
                  SNIPPET_CACHE_DIR=os.path.join(DATA, "cache"), METRICS_PORT="0")
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
