#!/bin/sh
# Re-encode the songs for the live site: music/<song>.mp3 (mostly 320 kbps, 957 MB)
# -> music_128/<song>.mp3 (128 kbps, ~400 MB). The originals stay as they are
# (the alignment pipeline uses them). Cover art is dropped; existing outputs are skipped.
#
# Usage (from lyrics/): ./reencode.sh [jobs]     then rsync music_128/ to the server
set -e
cd "$(dirname "$0")"
mkdir -p music_128
ls music/*.mp3 | xargs -P "${1:-4}" -I{} sh -c '
    out="music_128/$(basename "$1")"
    [ -e "$out" ] && exit 0
    ffmpeg -v error -i "$1" -map 0:a -map_metadata 0 -c:a libmp3lame -b:a 128k "$out.part.mp3" \
        && mv "$out.part.mp3" "$out" || echo "FAILED: $1"
' _ {}
du -sh music music_128
