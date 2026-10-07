#!/usr/bin/env python3
"""Smoke test of the built Docker image, run like in Kubernetes, on made-up data.

    python3 web/tests/smoke_test.py <image>

Creates fixture.py's database plus a generated tone per song (with the image's own
ffmpeg), starts the container read-only, as its non-root user, with the data mounted
read-only, then checks pages, clips, metrics, logs and the container's restrictions.
Exits 1 if any check fails. Needs only Docker and Python 3 (no app dependencies).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "extractos:test"
NAME = "extractos-smoke"
WEB, METRICS = "http://127.0.0.1:5055", "http://127.0.0.1:9155"
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def get(url, data=None, headers=None):
    """(status, headers, body) without raising on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kw)


def mp3_seconds(data, workdir):
    """Duration of an mp3 as the image's ffmpeg decodes it (None if it can't)."""
    path = os.path.join(workdir, "clip.mp3")
    with open(path, "wb") as f:
        f.write(data)
    r = docker("run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{workdir}:/w:ro", "--entrypoint",
               "ffmpeg", IMAGE, "-v", "error", "-i", "/w/clip.mp3", "-f", "null", "-", "-stats")
    times = re.findall(r"time=(\d+):(\d+):([\d.]+)", r.stderr)
    if r.returncode != 0 or not times:
        return None
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def main():
    work = tempfile.mkdtemp(prefix="extractos-smoke-")
    data = os.path.join(work, "data")
    os.makedirs(os.path.join(data, "music"))
    fixture.build_db(os.path.join(data, "redondos.db"))
    for song in fixture.SONGS:
        r = docker("run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{data}/music:/out", "--entrypoint",
                   "ffmpeg", IMAGE, "-v", "error", "-f", "lavfi", "-i",
                   f"sine=frequency=440:duration={fixture.SONG_SECONDS}", "-c:a", "libmp3lame", "-b:a", "64k",
                   f"/out/{song}.mp3")
        if r.returncode != 0:
            sys.exit(f"cannot make test audio with the image's ffmpeg: {r.stderr}")
    for root, dirs, files in os.walk(work):             # readable by the container's user (10001)
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)

    docker("rm", "-f", NAME)
    r = docker("run", "-d", "--name", NAME, "--read-only", "--tmpfs", "/cache", "--tmpfs", "/tmp",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-p", "127.0.0.1:5055:5000", "-p", "127.0.0.1:9155:9100", "-v", f"{data}:/data:ro", IMAGE)
    if r.returncode != 0:
        sys.exit(f"cannot start the container: {r.stderr}")
    try:
        run_checks(work)
    finally:
        logs = docker("logs", NAME).stdout
        if failures:
            print("\n--- container logs ---\n" + logs + docker("logs", NAME).stderr)
        docker("rm", "-f", NAME)
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


def run_checks(work):
    for _ in range(60):
        try:
            if get(WEB + "/healthz")[0] == 200:
                break
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    status, _, body = get(WEB + "/healthz")
    check("starts and answers /healthz", status == 200 and body == b"ok", f"{status} {body[:80]!r}")
    if status != 200:
        return

    status, _, body = get(WEB + "/")
    check("home page", status == 200 and b"Extractos" in body, str(status))

    status, _, body = get(WEB + "/search?lyric=sale+sobre")
    page = re.sub(r"\s+", " ", body.decode())
    check("search finds and highlights the line", "La luna <mark>sale</mark> <mark>sobre</mark> el puerto" in page)
    clip = re.search(r'src="(/clip/[^"]+\.mp3)"', page)
    check("search result has a clip", bool(clip))

    status, _, body = get(WEB + "/search?lyric=tanbor")
    check("typos are found", "<mark>tambor</mark>" in body.decode())

    status, _, body = get(WEB + "/suggest?q=vamos+a")
    check("suggestions", status == 200 and json.loads(body)[0]["text"] == "Vamos a brillar esta noche", body[:120])

    if clip:
        status, headers, body = get(WEB + clip.group(1))
        seconds = mp3_seconds(body, work) if status == 200 else None
        # "sale sobre" is 2 words of 0.4 s, plus 0.2 s before and 0.3 s after
        check("clip is a playable mp3 of the right length", seconds is not None and 1.0 <= seconds <= 1.6,
              f"status {status}, {headers.get('Content-Type')}, {len(body)} bytes, {seconds} s")
        status, headers, _ = get(WEB + clip.group(1) + "?name=Prueba.mp3")
        check("download sends an attachment", status == 200 and "attachment" in headers.get("Content-Disposition", ""))

    check("unknown song: 404", get(WEB + "/clip/no_such_song/1000-2000.mp3")[0] == 404)
    check("clip longer than 60 s: 404", get(WEB + "/clip/cancion_de_prueba/0-61000.mp3")[0] == 404)
    check("share event accepted", get(WEB + "/event", b'{"type":"share","result":"shared"}',
                                      {"Content-Type": "application/json"})[0] == 204)
    check("/metrics is not on the public port", get(WEB + "/metrics")[0] == 404)

    status, _, body = get(METRICS + "/metrics")
    text = body.decode()
    sample = lambda name: float(m.group(1)) if (m := re.search(re.escape(name) + r" ([\d.e+]+)", text)) else None
    check("metrics on port 9100", status == 200 and "extractos_http_requests_total" in text, str(status))
    check("metrics count the search", (sample('extractos_searches_total{outcome="exact"}') or 0) >= 1)
    check("metrics count the clip cut", (sample('extractos_clips_total{kind="play",result="cut"}') or 0) >= 1)
    check("metrics count the share", (sample('extractos_shares_total{result="shared"}') or 0) >= 1)
    check("metrics see the index", sample("extractos_index_songs") == len(fixture.SONGS))

    logs = docker("logs", NAME).stdout.splitlines()
    try:
        records = [json.loads(line) for line in logs if line.strip()]
        ok = all("event" in r for r in records)
    except ValueError as e:
        records, ok = [], False
        check("app logs are JSON lines", False, str(e))
    else:
        check("app logs are JSON lines", ok and bool(records))
    check("search is logged", any(r.get("event") == "search" and r.get("query") == "sale sobre" for r in records))

    check("runs as the non-root user 10001", docker("exec", NAME, "id", "-u").stdout.strip() == "10001")
    check("root filesystem is read-only", docker("exec", NAME, "touch", "/app/x").returncode != 0)


if __name__ == "__main__":
    main()
