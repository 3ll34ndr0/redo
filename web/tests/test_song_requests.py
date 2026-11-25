"""Songs visitors ask for: POST /request-song, the form on the page, the store."""

import os
import sys

import pytest

import app as app_module
from song_requests import SongRequests
from test_app import client, metric  # noqa: F401  (client: the fixture)



def ask(client, **body):
    return client.post("/request-song", json=body)


def stored(request_id):
    return [r for r in app_module.song_requests.all() if r["id"] == request_id][0]


def test_form_is_offered_only_without_an_exact_result(client):
    has_form = lambda q: 'id="request-song"' in client.get("/search?lyric=" + q).get_data(as_text=True)
    assert has_form("xyzzy+qwerty")               # nothing found
    assert has_form("la+luna+zale+zobre+el+puerto")     # only something similar
    assert not has_form("sale+sobre")             # found
    assert not has_form("")                       # nothing searched
    assert 'id="request-song"' not in client.get("/").get_data(as_text=True)


def test_request_is_stored_once_and_counted(client):
    new, again = (metric("extractos_song_requests_total", result=r) for r in ("new", "repeated"))
    r = ask(client, title="Tema  Inventado", artist="Los Ejemplo", query="una frase")
    assert r.status_code == 201
    row = stored(r.get_json()["id"])
    assert (row["title"], row["artist"], row["query"], row["times"], row["status"]) == \
        ("Tema Inventado", "Los Ejemplo", "una frase", 1, "open")
    r2 = ask(client, title="TEMA inventado!", artist="los ejémplo", query="otra")      # the same song, typed otherwise
    assert r2.get_json()["id"] == row["id"] and stored(row["id"])["times"] == 2
    assert ask(client, title="Tema Inventado", artist="Otra Banda").get_json()["id"] != row["id"]
    assert metric("extractos_song_requests_total", result="new") == new + 2
    assert metric("extractos_song_requests_total", result="repeated") == again + 1


def test_artist_is_optional_and_text_is_cleaned(client):
    row = stored(ask(client, title="a\x1b[31m\nb" + "x" * 500).get_json()["id"])
    assert row["artist"] == "" and len(row["title"]) == 120 and row["title"].startswith("a [31m b")


@pytest.mark.parametrize("body", [{}, {"title": "  "}, {"title": "?!"}, {"title": 5}, {"title": None}, {"title": ["x"]}])
def test_bad_requests_are_refused(client, body):
    n = len(app_module.song_requests.all())
    assert ask(client, **body).status_code == 400
    assert len(app_module.song_requests.all()) == n


def test_only_from_our_page(client):
    assert client.post("/request-song", data={"title": "x"}).status_code == 403
    assert client.post("/request-song", data="{}", content_type="text/plain").status_code == 403
    assert client.post("/request-song", json={"title": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/request-song", data="nope", content_type="application/json").status_code == 400


def test_without_storage(client, monkeypatch):
    monkeypatch.setattr(app_module, "song_requests", None)
    assert ask(client, title="x").status_code == 503
    assert 'id="request-song"' not in client.get("/search?lyric=xyzzy").get_data(as_text=True)


def test_tool_lists_and_closes(tmp_path, monkeypatch, capsys):
    import subprocess
    import song_requests as _web     # tools/song_requests.py has the same name: load it by path
    import importlib.util
    path = os.path.join(os.path.dirname(_web.__file__), "..", "tools", "song_requests.py")
    spec = importlib.util.spec_from_file_location("song_requests_tool", path)
    import types
    db = str(tmp_path / "r.db")
    store = SongRequests(db)
    first, _ = store.add("Uno", "Banda", "q")
    fake = types.SimpleNamespace(in_pod=lambda cmd, stdin=None: subprocess.run(
        [sys.executable, "-"], input=stdin.replace("/reports/reports.db", db), capture_output=True, text=True))
    monkeypatch.setitem(sys.modules, "reports", fake)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    run = lambda *a: (monkeypatch.setattr(sys, "argv", ["x", *a]), tool.main(), capsys.readouterr().out)[2]
    assert "Uno — Banda" in run("list")
    assert "1 of 1 closed as published" in run("close", str(first), "--as", "published")
    assert run("list").startswith("0 requests")
    assert "[published]" in run("list", "--all")
    assert store.add("Uno", "Banda", "q")[1]              # closed: asking again is a new request
