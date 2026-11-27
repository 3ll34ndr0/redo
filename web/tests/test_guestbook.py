"""Libro de visitas: POST /guestbook (voice → text, Orchardrun faked), the section on the page, /admin/messages."""

import io

import pytest

import app as app_module
import guestbook
import stt
import tokens
from test_app import client, metric  # noqa: F401  (client: the fixture)


def write(client, text, **extra):
    return client.post("/guestbook/text", json={"text": text, **extra})


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
    assert 'id="mensajes"' in page and 'class="gb-mic"' not in page
    assert 'data-voice="0"' in page and 'class="gb-write"' in page            # the script offers writing instead
    assert client.post("/guestbook", data=message()).status_code == 404
    assert write(client, "Sin micrófono, un saludo igual").status_code == 202  # writing doesn't need a key


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


# --- written messages: the fallback, kept in quarantine

def test_a_written_message_waits_in_quarantine_exactly_as_typed(client, capsys):
    raw = "Hola <script>alert('x')</script>\n'); DROP TABLE guestbook;-- visitá https://spam.example.com \u202eabc"
    before = metric("extractos_guestbook_written_total", outcome="quarantined")
    voices = len(app_module.guestbook_store.all())
    r = write(client, raw)
    assert r.status_code == 202 and r.get_json() == {"status": "quarantined"}
    row = app_module.guestbook_store.quarantined()[0]
    assert row["raw"] == raw and row["status"] == "pending"                    # stored verbatim (bound, not run)
    assert row["flags"].split() == ["enlace", "html", "sql", "invisible"]
    assert len(app_module.guestbook_store.all()) == voices                     # nothing in the guestbook itself
    assert metric("extractos_guestbook_written_total", outcome="quarantined") == before + 1
    assert "alert" not in capsys.readouterr().out                              # never in the logs
    page = client.get("/").get_data(as_text=True)
    assert "alert(" not in page and "spam.example" not in page                 # not on the page


def test_written_messages_are_refused_when_malformed(client):
    n = len(app_module.guestbook_store.quarantined())
    assert client.post("/guestbook/text", data={"text": "un form"}).status_code == 403          # JSON only
    assert client.post("/guestbook/text", json={"text": "hola"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/guestbook/text", json={"text": "hola"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert write(client, "   ").status_code == 400
    assert client.post("/guestbook/text", json={"text": ["no"]}).status_code == 400
    assert client.post("/guestbook/text", json=["text"]).status_code == 400
    assert write(client, "x" * (guestbook.MAX_WRITTEN + 1)).status_code == 413               # refused, not cut
    assert client.post("/guestbook/text", json={"text": "y" * 9000}).status_code == 413
    assert len(app_module.guestbook_store.quarantined()) == n


def test_bots_repeats_and_a_full_quarantine(client, monkeypatch):
    n = len(app_module.guestbook_store.quarantined())
    assert write(client, "compre ya", website="http://bot.example").status_code == 202     # the hidden field: dropped
    assert len(app_module.guestbook_store.quarantined()) == n
    assert write(client, "Mismo texto otra vez").status_code == 202
    assert write(client, "Mismo texto otra vez").status_code == 202
    assert len(app_module.guestbook_store.quarantined()) == n + 1                           # kept once
    pending = sum(r["status"] == "pending" for r in app_module.guestbook_store.quarantined())
    monkeypatch.setattr(guestbook, "QUARANTINE_MAX_PENDING", pending)
    assert write(client, "Uno más de los que entran").status_code == 503


def test_admin_examines_and_publishes_only_a_cleaned_copy(client):
    write(client, 'Gracias por "la página" <img src=x onerror=alert(1)>\u200b\n\nsaludos')
    row = app_module.guestbook_store.quarantined()[0]
    url = f"/admin/messages/written/{row['id']}/decision"
    assert client.get("/admin/messages?show=quarantine").status_code == 404                   # no token
    assert client.post(url, json={"decision": "publish"}).status_code == 404
    admin = tokens.headers()
    page = client.get("/admin/messages?show=quarantine", headers=admin).get_data(as_text=True)
    assert f'data-written-id="{row["id"]}"' in page
    assert "<img src=x" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page          # escaped there too
    assert "[U+200B]" in page and ">HTML<" in page
    assert "<img src=x" not in client.get("/admin/messages", headers=admin).get_data(as_text=True)

    decide = lambda d, **h: client.post(url, json={"decision": d}, headers={**admin, **h})
    assert decide("publish", Origin="https://evil.example").status_code == 403
    assert decide("approve").status_code == 400
    assert client.post("/admin/messages/written/999999/decision", json={"decision": "discard"}, headers=admin).status_code == 404
    r = decide("publish")
    assert r.status_code == 200 and r.get_json()["status"] == "published"
    published = app_module.guestbook_store.get(app_module.guestbook_store.quarantined_one(row["id"])["message_id"])
    assert published["status"] == "approved" and published["seconds"] is None
    assert published["text"] == 'Gracias por "la página" <img src=x onerror=alert(1)> saludos'   # one line, no U+200B
    page = client.get("/").get_data(as_text=True)
    assert "Gracias por &#34;la página&#34; &lt;img src=x onerror=alert(1)&gt; saludos" in page
    assert "<img src=x" not in page
    assert decide("publish").status_code == 200                                               # again: the same copy
    assert len([m for m in app_module.guestbook_store.all() if m["text"].startswith("Gracias por")]) == 1
    assert decide("discard").get_json()["status"] == "discarded"
    assert "Gracias por" not in client.get("/").get_data(as_text=True)                        # hidden again


def test_nothing_printable_is_never_published(client):
    write(client, "\u202e\u200b\u2066")
    row = app_module.guestbook_store.quarantined()[0]
    assert row["flags"] == "invisible"
    r = client.post(f"/admin/messages/written/{row['id']}/decision", json={"decision": "publish"}, headers=tokens.headers())
    assert r.status_code == 404


def test_discarded_written_messages_are_deleted_later(tmp_path, monkeypatch):
    store = guestbook.Guestbook(str(tmp_path / "g.db"))
    old, _ = store.quarantine("spam viejo")
    store.release(old, "discard", "admin@example.com")
    real = guestbook.time.time
    monkeypatch.setattr(guestbook.time, "time", lambda: real() + 31 * 86400)
    store.quarantine("otro")
    assert store.quarantined_one(old) is None

