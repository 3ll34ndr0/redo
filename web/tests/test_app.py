"""The Flask app (web/app.py) on made-up data: pages, validation, clips, metrics."""

import json
import re

import pytest
from prometheus_client import REGISTRY

import app as app_module
from conftest import HAS_FFMPEG


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    app_module.limiter.enabled = False
    return app_module.app.test_client()


def metric(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0


def test_healthz(client):
    assert client.get("/healthz").data == b"ok"


def test_search_page_highlights_the_line(client):
    page = re.sub(r"\s+", " ", client.get("/search?lyric=sale+sobre").get_data(as_text=True))  # as a browser shows it
    assert "La luna <mark>sale</mark> <mark>sobre</mark> el puerto" in page
    assert "Cancion De Prueba" in page
    assert "/clip/cancion_de_prueba/" in page
    assert 'download="Cancion De Prueba - sale sobre.mp3"' in page


def test_search_without_results(client):
    page = client.get("/search?lyric=xyzzy+qwerty").get_data(as_text=True)
    assert "No se encontraron resultados" in page


def test_approximate_results_say_so(client):
    page = client.get("/search?lyric=puerto+de+la+luna").get_data(as_text=True)
    assert "Lo más parecido" in page


def test_searches_are_counted_by_outcome(client):
    before = metric("extractos_searches_total", outcome="none")
    client.get("/search?lyric=xyzzy")
    assert metric("extractos_searches_total", outcome="none") == before + 1


def test_counters_exist_before_first_use():
    # increase() in Prometheus misses the first event of a series that starts at 1
    for outcome in ("exact", "partial", "approx", "none"):
        assert REGISTRY.get_sample_value("extractos_searches_total", {"outcome": outcome}) is not None
    assert REGISTRY.get_sample_value("extractos_shares_total", {"result": "failed"}) is not None


def test_suggest(client):
    assert client.get("/suggest?q=vamos+a").get_json() == [
        {"text": "Vamos a brillar esta noche", "song": "Cancion De Prueba"}]


@pytest.mark.parametrize("url", [
    "/clip/no_such_song/1000-2000.mp3",            # unknown song
    "/clip/cancion_de_prueba/3000-2000.mp3",       # ends before it starts
    "/clip/cancion_de_prueba/0-61000.mp3",         # longer than 60 s
    "/clip/..%2Fetc/1-2.mp3",                      # path tricks
])
def test_bad_clip_requests(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg (the smoke test covers the image's)")
def test_clip_and_download(client):
    r = client.get("/clip/cancion_de_prueba/5000-7000.mp3")
    assert r.status_code == 200 and r.mimetype == "audio/mpeg" and len(r.data) > 1000
    assert "max-age=86400" in r.headers["Cache-Control"]
    r = client.get("/clip/cancion_de_prueba/5000-7000.mp3?name=Prueba%20-%20vamos.mp3")
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert metric("extractos_clips_total", result="cached", kind="download") >= 1


def test_share_events(client):
    before = metric("extractos_shares_total", result="shared")
    assert client.post("/event", data=json.dumps({"type": "share", "result": "shared"})).status_code == 204
    assert metric("extractos_shares_total", result="shared") == before + 1
    assert client.post("/event", data=json.dumps({"type": "share", "result": "hacked"})).status_code == 400
    assert client.post("/event", data="not json").status_code == 400


@pytest.mark.parametrize("tokens, name", [
    ([("Vamos", True, False), ("a", True, False), ("brillar,", True, False)], "Song - Vamos a brillar.mp3"),
    ([("¿Dónde/../", True, False), ("x", False, False)], "Song - Dónde.mp3"),
    ([("nada", False, False)], "Song.mp3"),
])
def test_file_name_is_safe(tokens, name):
    assert app_module.file_name("song", tokens) == name


def test_logs_are_json(client, capsys):
    client.get("/search?lyric=la+luna+sa")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    search = [l for l in lines if l["event"] == "search"][-1]
    assert search["query"] == "la luna sa" and search["outcome"] == "partial"
    assert not any("ip" in key.lower() for l in lines for key in l)    # no visitor addresses
