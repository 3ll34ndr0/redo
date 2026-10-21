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


def test_link_preview_tags(client):
    """Chat apps (WhatsApp, Telegram...) build the link preview from these; image and url must be absolute."""
    page = client.get("/").get_data(as_text=True)
    assert '<meta property="og:image" content="http://localhost/static/og.png">' in page
    assert '<meta name="twitter:card" content="summary_large_image">' in page
    assert client.get("/static/og.png").status_code == 200


def test_link_preview_of_a_search_shows_the_phrase(client, monkeypatch):
    monkeypatch.setattr(app_module, "SITE_URL", "https://unaudio.example")
    page = client.get('/search?lyric=sale+sobre+"<b>').get_data(as_text=True)
    assert '<meta property="og:title" content="“sale sobre &#34;&lt;b&gt;” en una canción de los Redondos">' in page
    assert '<meta property="og:url" content="https://unaudio.example/search?lyric=sale%20sobre%20%22%3Cb%3E">' in page
    assert '<meta property="og:image" content="https://unaudio.example/static/og.png">' in page
    page = client.get("/search?lyric=sale+sobre").get_data(as_text=True)
    assert "Escuchala en Cancion De Prueba" in page


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



@pytest.mark.parametrize("url", [
    "/voice/no_such_song/1000-2000.wav",           # unknown song
    "/voice/cancion_de_prueba/3000-2000.wav",      # ends before it starts
    "/voice/cancion_de_prueba/0-71000.wav",        # longer than the editor's window (70 s)
])
def test_bad_voice_requests(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg (the smoke test covers the image's)")
@pytest.mark.parametrize("song, source", [("cancion_de_prueba", "vocals"), ("otra_cancion", "song")])
def test_voice_window(client, song, source):
    """The timing editor's audio: exact length, mono 16 kHz WAV, from the vocal stem if there is one."""
    import io
    import wave
    before = metric("extractos_clips_total", result="cut", kind="voice")
    r = client.get(f"/voice/{song}/4300-15100.wav")
    assert r.status_code == 200 and r.mimetype == "audio/wav"
    assert r.headers["X-Voice-Source"] == source
    with wave.open(io.BytesIO(r.data)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        assert abs(w.getnframes() / 16000 - 10.8) < 0.01      # sample 0 is 4.3 s, no padding
    assert metric("extractos_clips_total", result="cut", kind="voice") == before + 1

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

@pytest.fixture(autouse=True)
def no_provisional_fixes():
    """Timing reports move the index's words at once: put them back after each test."""
    yield
    app_module.reports.set_status([r["id"] for r in app_module.reports.applied()], "revoked")
    app_module._index["checked"] = 0.0
    app_module._auto_applied.clear()


def post_report(client, **fields):
    body = {"song": "cancion_de_prueba", "start_ms": 5000, "end_ms": 7000, "problem": "timing"}
    body.update(fields)
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/report", data=json.dumps(body), content_type="application/json")


def test_report_is_stored(client):
    before = metric("extractos_clip_reports_total", problem="timing")
    r = post_report(client, fixed_start_ms=4600, fixed_end_ms=7200, query="vamos a brillar",
                    line="Vamos a brillar esta noche")
    assert r.status_code == 201
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert stored["song"] == "cancion_de_prueba" and stored["problem"] == "timing"
    assert (stored["start_ms"], stored["end_ms"], stored["fixed_start_ms"], stored["fixed_end_ms"]) == (5000, 7000, 4600, 7200)
    assert stored["line"] == "Vamos a brillar esta noche" and stored["db_version"]
    assert metric("extractos_clip_reports_total", problem="timing") == before + 1


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
    assert "Está mal cortado" in page and "Quisió... (No sabe, no responde)" in page


def test_every_problem_is_offered_accepted_and_counted(client):
    from reports import PROBLEMS
    page = client.get("/search?lyric=sale+sobre").get_data(as_text=True)
    for problem in PROBLEMS:
        assert f'value="{problem}"' in page                          # the page offers it
        before = metric("extractos_clip_reports_total", problem=problem)
        assert post_report(client, problem=problem).status_code == 201
        assert metric("extractos_clip_reports_total", problem=problem) == before + 1


# --- provisional timing fixes: a timing report is applied at once, until it's reviewed

def clip_of(client, query):
    page = client.get("/search?lyric=" + query).get_data(as_text=True)
    return re.search(r'/clip/cancion_de_prueba/(\d+-\d+)\.mp3', page).group(1)


def test_timing_report_is_applied_at_once_and_revoked_on_review(client):
    assert clip_of(client, "sale+sobre") == "9300-10100"
    r = post_report(client, start_ms=9300, end_ms=10100, fixed_start_ms=9000, fixed_end_ms=10100)
    assert r.get_json()["provisional"] is True
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert (stored["first_tok"], stored["last_tok"], stored["first_word"], stored["last_word"]) == (7, 8, "sale", "sobre")
    assert stored["status"] == "provisional"
    assert clip_of(client, "sale+sobre") == "9000-10100"       # no wait: the reporter sees it
    app_module.reports.set_status([stored["id"]], "revoked")
    assert clip_of(client, "sale+sobre") == "9000-10100"       # reviews are picked up every REPORTS_RECHECK_S
    app_module._index["checked"] = 0.0
    assert clip_of(client, "sale+sobre") == "9300-10100"


def test_accepted_reports_stay_applied(client):
    r = post_report(client, start_ms=9300, end_ms=10100, fixed_start_ms=9300, fixed_end_ms=10400)
    app_module.reports.set_status([r.get_json()["id"]], "accepted")
    app_module._index["checked"] = 0.0
    assert clip_of(client, "sale+sobre") == "9300-10400"


def test_reports_on_an_adjusted_clip_take_the_median(client):
    post_report(client, start_ms=9300, end_ms=10100, fixed_start_ms=9000, fixed_end_ms=10100)
    # the next visitor gets the adjusted clip, and nudges it
    post_report(client, start_ms=9000, end_ms=10100, fixed_start_ms=9200, fixed_end_ms=10100)
    assert clip_of(client, "sale+sobre") == "9100-10100"


@pytest.mark.parametrize("fields", [
    {"problem": "wrong_phrase", "fixed_start_ms": 9000, "fixed_end_ms": 10100},
    {"problem": "timing"},                                          # no adjustment
    {"problem": "timing", "fixed_start_ms": 9300, "fixed_end_ms": 10100},   # adjustment = as served
])
def test_only_timing_adjustments_are_applied(client, fields):
    r = post_report(client, start_ms=9300, end_ms=10100, **fields)
    assert r.get_json()["provisional"] is False
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert stored["status"] is None and stored["first_tok"] == 7       # words still recorded for the review
    assert clip_of(client, "sale+sobre") == "9300-10100"


def test_a_clip_that_matches_no_words_is_not_applied(client):
    # e.g. a page from before the database changed
    r = post_report(client, start_ms=9350, end_ms=10100, fixed_start_ms=9000, fixed_end_ms=10100)
    assert r.get_json()["provisional"] is False
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    assert stored["status"] is None and stored["first_tok"] is None


def test_reports_baked_into_the_database_are_not_applied_again(client, tmp_path, monkeypatch):
    import shutil
    import sqlite3
    r = post_report(client, start_ms=9300, end_ms=10100, fixed_start_ms=9000, fixed_end_ms=10100)
    stored = [x for x in app_module.reports.all() if x["id"] == r.get_json()["id"]][0]
    db = tmp_path / "redondos.db"
    shutil.copy(app_module.DB_PATH, db)
    with sqlite3.connect(db) as conn:       # as build_db.py writes it, without the timing (the test only needs the key)
        conn.execute("CREATE TABLE fixes (report TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO fixes VALUES (?)", (f"{stored['id']}@{stored['created_at']}",))
    monkeypatch.setattr(app_module, "DB_PATH", str(db))
    assert clip_of(client, "sale+sobre") == "9300-10100"
    monkeypatch.undo()
    app_module._index["mtime"] = None       # back to the fixture's database


def test_old_reports_database_gets_the_new_columns(tmp_path):
    import sqlite3
    from reports import ReportStore
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                     " song TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, problem TEXT NOT NULL,"
                     " fixed_start_ms INTEGER, fixed_end_ms INTEGER, query TEXT, line TEXT, db_version TEXT)")
        conn.execute("INSERT INTO reports (created_at, song, start_ms, end_ms, problem, fixed_start_ms, fixed_end_ms)"
                     " VALUES ('2026-10-01T00:00:00Z', 's', 1000, 2000, 'timing', 900, 2000)")
    store = ReportStore(str(path))
    old = store.all()[0]
    assert old["status"] is None and old["first_tok"] is None       # reviewed by hand already: never applied
    assert store.applied() == []


def test_kill_switch(client, monkeypatch):
    post_report(client, start_ms=9300, end_ms=10100, fixed_start_ms=9000, fixed_end_ms=10100)
    assert clip_of(client, "sale+sobre") == "9000-10100"
    monkeypatch.setattr(app_module, "AUTO_APPLY", False)
    r = post_report(client, start_ms=9000, end_ms=10100, fixed_start_ms=9000, fixed_end_ms=10500)
    assert r.get_json()["provisional"] is False                 # new reports wait for a review
    app_module._index["checked"] = 0.0
    assert clip_of(client, "sale+sobre") == "9300-10100"        # and the stored ones stop being applied


def test_a_far_move_waits_for_a_review(client):
    r = post_report(client, start_ms=9300, end_ms=10100, fixed_start_ms=4200, fixed_end_ms=10100)
    assert r.get_json()["provisional"] is False
    assert clip_of(client, "sale+sobre") == "9300-10100"


def test_provisional_fixes_per_visitor_per_day(client, monkeypatch):
    monkeypatch.setattr(app_module, "AUTO_APPLY_PER_DAY", 2)
    you, other = {"CF-Connecting-IP": "203.0.113.1"}, {"CF-Connecting-IP": "203.0.113.2"}
    lines = [(5000, 7000), (8500, 10900), (12400, 14400), (15900, 17900)]      # the fixture's first four lines

    def send(clip, headers):
        body = {"song": "cancion_de_prueba", "start_ms": clip[0], "end_ms": clip[1], "problem": "timing",
                "fixed_start_ms": clip[0] - 100, "fixed_end_ms": clip[1]}
        return client.post("/report", headers=headers, data=json.dumps(body), content_type="application/json").get_json()
    assert [send(c, you)["provisional"] for c in lines[:3]] == [True, True, False]
    assert send(lines[3], other)["provisional"] is True        # someone else still can


def test_every_module_goes_into_the_image():
    """The Dockerfile copies the app's modules (and the pages' scripts) one by one, and .dockerignore lets only those through:
    a new module missing from either only shows when the image fails to start."""
    import glob
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    modules = {os.path.relpath(p, os.path.join(root, "web")) for pattern in ("*.py", "static/*.js")
               for p in glob.glob(os.path.join(root, "web", pattern))}
    dockerfile = open(os.path.join(root, "Dockerfile")).read()
    dockerignore = open(os.path.join(root, ".dockerignore")).read().split()
    for name in modules:
        assert f"web/{name}" in dockerfile, f"{name} isn't copied by the Dockerfile"
        assert f"!web/{name}" in dockerignore, f"{name} isn't let through by .dockerignore"


def test_scripts_have_their_version_in_the_url(client):
    page = client.get("/").get_data(as_text=True)
    url = re.search(r'src="(/static/wave\.js\?v=[0-9a-f]{12})"', page).group(1)
    assert client.get(url).status_code == 200
