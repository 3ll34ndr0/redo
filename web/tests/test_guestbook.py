"""Libro de visitas: POST /guestbook (voice → text, Orchardrun faked), the section on the page, /admin/messages."""

import io

import pytest

import app as app_module
import guestbook
import stt
import tokens
from test_app import client, metric  # noqa: F401  (client: the fixture)


def message(data=b"fake opus", mimetype="audio/webm", seconds="7.2"):
    return {"audio": (io.BytesIO(data), "mensaje.webm", mimetype), "seconds": seconds}


@pytest.fixture
def heard(monkeypatch):
    """The guestbook's own key, Orchardrun faked: answers said["text"] (or raises it); calls recorded."""
    calls, said = [], {"text": "Hola, falta  Ji ji ji\x07"}
    monkeypatch.setenv("GUESTBOOK_ORCHARDRUN_API_KEY", "guestbook-key")
    monkeypatch.setattr(stt, "_no_credit_until", {})

    def fake(audio, filename, content_type, key_env=stt.KEY_ENV):
        calls.append((audio, content_type, key_env))
        if isinstance(said["text"], Exception):
            raise said["text"]
        return said["text"]
    monkeypatch.setattr(stt, "transcribe", fake)
    return calls, said


def test_a_message_is_transcribed_with_its_own_key_and_stored_pending(client, heard):
    calls, _ = heard
    before = metric("extractos_guestbook_messages_total", outcome="stored")
    r = client.post("/guestbook", data=message())
    assert r.status_code == 201
    body = r.get_json()
    assert body["text"] == "Hola, falta Ji ji ji"                 # one line, no control characters
    assert calls == [(b"fake opus", "audio/webm", "GUESTBOOK_ORCHARDRUN_API_KEY")]
    row = app_module.guestbook_store.get(body["id"])
    assert (row["status"], row["text"], row["seconds"]) == ("pending", "Hola, falta Ji ji ji", 7.2)
    assert app_module.guestbook_store.audio(body["id"]) == ("audio/webm", b"fake opus")
    assert metric("extractos_guestbook_messages_total", outcome="stored") == before + 1
    assert "Hola, falta" not in client.get("/").get_data(as_text=True)      # not shown until approved


def test_without_its_own_key_it_uses_the_mic_key(client, heard, monkeypatch):
    monkeypatch.delenv("GUESTBOOK_ORCHARDRUN_API_KEY")
    monkeypatch.setenv("ORCHARDRUN_API_KEY", "mic-key")
    assert client.post("/guestbook", data=message()).status_code == 201
    assert heard[0][-1][2] == "ORCHARDRUN_API_KEY"


def test_no_key_no_recording(client, monkeypatch):
    monkeypatch.delenv("GUESTBOOK_ORCHARDRUN_API_KEY", raising=False)
    monkeypatch.delenv("ORCHARDRUN_API_KEY", raising=False)
    page = client.get("/").get_data(as_text=True)
    assert 'id="mensajes"' in page and 'class="gb-mic"' not in page and "no se pueden dejar mensajes" in page
    assert client.post("/guestbook", data=message()).status_code == 404


def test_the_section_and_the_button(client, heard):
    page = client.get("/").get_data(as_text=True)
    assert 'id="mensajes"' in page and 'class="gb-mic"' in page and "/static/guestbook.js?v=" in page
    assert 'href="#mensajes"' in page                              # the hint to scroll down
    assert 'id="mensajes"' in client.get("/search?lyric=sale").get_data(as_text=True)


def test_nothing_heard_is_not_stored(client, heard):
    heard[1]["text"] = "  "
    n = len(app_module.guestbook_store.all())
    r = client.post("/guestbook", data=message())
    assert r.status_code == 200 and r.get_json() == {"text": ""}
    assert len(app_module.guestbook_store.all()) == n


def test_service_down_and_out_of_credit(client, heard, monkeypatch):
    calls, said = heard
    said["text"] = stt.Unavailable("HTTP 500: details")
    r = client.post("/guestbook", data=message())
    assert r.status_code == 502 and "details" not in r.get_data(as_text=True)

    def out_of_credit(audio, filename, content_type, key_env):     # what stt.transcribe does on a 402
        stt._no_credit_until[key_env] = stt.time.time() + stt.NO_CREDIT_PAUSE_S
        raise stt.NoCredit("HTTP 402")
    monkeypatch.setattr(stt, "transcribe", out_of_credit)
    monkeypatch.setenv("ORCHARDRUN_API_KEY", "mic-key")
    assert client.post("/guestbook", data=message()).status_code == 503
    page = client.get("/").get_data(as_text=True)
    assert 'class="gb-mic"' not in page                                    # the guestbook's key is paused…
    assert 'id="mic"' in page                                      # …the search mic's isn't


@pytest.mark.parametrize("data", [{}, message(mimetype="text/plain"), message(data=b"")])
def test_bad_recordings_are_refused(client, heard, data):
    assert client.post("/guestbook", data=data).status_code == 400
    assert heard[0] == []


def test_long_recordings_are_refused(client, heard):
    r = client.post("/guestbook", data=message(data=b"x" * (app_module.MAX_MESSAGE_BYTES + 1)))
    assert r.status_code == 413 and heard[0] == []


def test_posts_from_other_sites_are_refused(client, heard):
    assert client.post("/guestbook", data=message(), headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/guestbook", data=message(), headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert heard[0] == []


def test_admin_approves_and_the_text_is_shown_never_the_audio(client, heard):
    heard[1]["text"] = "Un saludo <b>grande</b> desde Olavarría"
    message_id = client.post("/guestbook", data=message()).get_json()["id"]
    assert client.get("/admin/messages").status_code == 404                            # no token
    assert client.get(f"/admin/messages/{message_id}/audio").status_code == 404
    admin = tokens.headers()
    page = client.get("/admin/messages", headers=admin).get_data(as_text=True)
    assert f'data-id="{message_id}"' in page and "&lt;b&gt;grande" in page
    audio = client.get(f"/admin/messages/{message_id}/audio", headers=admin)
    assert audio.status_code == 200 and audio.data == b"fake opus" and audio.mimetype == "audio/webm"

    decide = lambda d, **h: client.post(f"/admin/messages/{message_id}/decision", json={"decision": d},
                                        headers={**admin, **h})
    assert decide("approve", Origin="https://evil.example").status_code == 403
    assert decide("delete").status_code == 400
    r = decide("approve")
    assert r.status_code == 200 and r.get_json()["status"] == "approved"
    page = client.get("/").get_data(as_text=True)
    assert "Un saludo &lt;b&gt;grande&lt;/b&gt; desde Olavarría" in page              # escaped
    assert f"/admin/messages/{message_id}/audio" not in page
    assert decide("hide").get_json()["status"] == "hidden"
    assert "desde Olavarría" not in client.get("/").get_data(as_text=True)
    row = app_module.guestbook_store.get(message_id)
    assert row["reviewed_by"] and row["reviewed_at"]


def test_old_recordings_are_deleted(tmp_path, monkeypatch):
    store = guestbook.Guestbook(str(tmp_path / "g.db"))
    old = store.add("viejo", 3.0, b"old", "audio/webm")
    real = guestbook.time.time
    monkeypatch.setattr(guestbook.time, "time", lambda: real() + 31 * 86400)
    new = store.add("nuevo", 3.0, b"new", "audio/webm")
    assert store.audio(old) is None and store.audio(new) == ("audio/webm", b"new")
    assert store.get(old)["text"] == "viejo"                       # the text stays
