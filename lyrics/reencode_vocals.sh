#!/bin/sh
# Light copies of the Demucs vocal stems for the live site's timing editor (¿No coincide?):
# separated/htdemucs_ft/<song>/vocals.mp3 (256 kbps stereo, ~774 MB)
# -> vocals_64/<song>.mp3 (mono 64 kbps, ~100 MB). Same timeline as the stems (and the
# songs: stems are +0.025 s, ignored). Existing outputs are skipped.
#
# Usage (from lyrics/): ./reencode_vocals.sh [jobs]     then rsync vocals_64/ to the server
set -e
cd "$(dirname "$0")"
mkdir -p vocals_64
ls separated/htdemucs_ft/*/vocals.mp3 | xargs -P "${1:-4}" -I{} sh -c '
    song=$(basename "$(dirname "$1")")
    out="vocals_64/$song.mp3"
    [ -e "$out" ] && exit 0
    ffmpeg -v error -i "$1" -map 0:a -map_metadata -1 -ac 1 -c:a libmp3lame -b:a 64k "$out.part.mp3" \
        && mv "$out.part.mp3" "$out" || echo "FAILED: $1"
' _ {}
du -sh vocals_64
