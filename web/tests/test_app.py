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


# --- clip reports (¿No coincide?)

def post_report(client, **fields):
    body = {"song": "cancion_de_prueba", "start_ms": 5000, "end_ms": 7000, "problem": "starts_late"}
    body.update(fields)
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/report", data=json.dumps(body), content_type="application/json")


def test_report_is_stored(client):
    before = metric("extractos_clip_reports_total", problem="starts_late")
    r = post_report(client, fixed_start_ms=4600, fixed_end_ms=7200, query="vamos a brillar",
                    line="Vamos a brillar esta noche")
    assert r.status_code == 201
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert stored["song"] == "cancion_de_prueba" and stored["problem"] == "starts_late"
    assert (stored["start_ms"], stored["end_ms"], stored["fixed_start_ms"], stored["fixed_end_ms"]) == (5000, 7000, 4600, 7200)
    assert stored["line"] == "Vamos a brillar esta noche" and stored["db_version"]
    assert metric("extractos_clip_reports_total", problem="starts_late") == before + 1


def test_report_without_adjustment(client):
    r = post_report(client, problem="wrong_phrase")
    assert r.status_code == 201
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert stored["fixed_start_ms"] is None and stored["fixed_end_ms"] is None


@pytest.mark.parametrize("fields", [
    {"song": "no_such_song"},
    {"problem": "too_loud"},
    {"start_ms": 7000, "end_ms": 5000},                  # end before start
    {"start_ms": 0, "end_ms": 61000},                    # longer than 60 s
    {"start_ms": "5000"},                                # not an integer
    {"start_ms": -1},
    {"start_ms": True},
    {"fixed_start_ms": 4000},                            # fixed times go together
    {"fixed_start_ms": 9000, "fixed_end_ms": 8000},
])
def test_bad_reports_are_refused(client, fields):
    count = len(app_module.reports.all())
    assert post_report(client, **fields).status_code == 400
    assert len(app_module.reports.all()) == count       # nothing stored


def test_report_text_is_cut(client):
    r = post_report(client, query="x" * 1000, line="y" * 1000)
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert len(stored["query"]) == 200 and len(stored["line"]) == 200


def test_report_not_json(client):
    assert client.post("/report", data="nope").status_code == 400
    assert client.post("/report", data="[1, 2]", content_type="application/json").status_code == 400


def test_card_has_the_report_panel(client):
    page = client.get("/search?lyric=sale+sobre").get_data(as_text=True)
    assert "¿No coincide?" in page and "Escuchar el ajuste" in page
    assert "Termina tarde" in page and "Quisió... (No sabe, no responde)" in page


def test_every_problem_is_offered_accepted_and_counted(client):
    from reports import PROBLEMS
    page = client.get("/search?lyric=sale+sobre").get_data(as_text=True)
    for problem in PROBLEMS:
        assert f'value="{problem}"' in page                          # the page offers it
        before = metric("extractos_clip_reports_total", problem=problem)
        assert post_report(client, problem=problem).status_code == 201
        assert metric("extractos_clip_reports_total", problem=problem) == before + 1
