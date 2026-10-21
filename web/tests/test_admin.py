"""The /admin pages: only with a valid Cloudflare Access token (access.py), here signed by the
test key (tokens.py); the report list."""

import base64
import hashlib
import hmac
import json
import re
import time

import jwt
import pytest
from prometheus_client import REGISTRY
from cryptography.hazmat.primitives import serialization

import access
import app as app_module
import tokens


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    app_module.limiter.enabled = False
    return app_module.app.test_client()


@pytest.fixture
def report_ids():
    """Three reports: one still to review, one accepted, one about another song."""
    store = app_module.reports
    ids = [store.add({"song": "cancion_de_prueba", "start_ms": 5000, "end_ms": 7000, "problem": "timing",
                      "fixed_start_ms": 4700, "fixed_end_ms": 7000, "line": "Línea de prueba <b>uno</b>",
                      "query": "prueba"}),
           store.add({"song": "cancion_de_prueba", "start_ms": 9300, "end_ms": 10100, "problem": "wrong_phrase"}),
           store.add({"song": "otra_cancion", "start_ms": 1000, "end_ms": 2500, "problem": "ends_late"})]
    store.set_status([ids[1]], "accepted")
    yield ids
    store.set_status(ids, "revoked")


def shown(page):
    return {int(x) for x in re.findall(r'class="report [^"]*" data-id="(\d+)"', page)}


@pytest.mark.parametrize("url", ["/admin", "/admin/reports", "/admin/reports?show=all"])
def test_without_a_token_admin_pages_dont_exist(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("bad", [
    {"aud": ["another-app"]},                            # a token for another Access application
    {"iss": "https://evil.cloudflareaccess.com"},
    {"exp": int(time.time()) - 120},                     # expired (beyond the 30 s leeway)
    {"exp": None},
    {"key": tokens.OTHER_KEY},                           # right key id, wrong key: forged
    {"kid": "unknown-key"},
])
def test_invalid_tokens_are_refused(client, bad):
    assert client.get("/admin/reports", headers=tokens.headers(**bad)).status_code == 404


def test_unsigned_and_symmetric_tokens_are_refused(client):
    body = {"aud": [tokens.AUD], "iss": f"https://{tokens.TEAM}", "email": tokens.EMAIL,
            "iat": int(time.time()), "exp": int(time.time()) + 600}
    unsigned = jwt.encode(body, None, algorithm="none", headers={"kid": tokens.KID})
    assert client.get("/admin/reports", headers={access.HEADER: unsigned}).status_code == 404
    # HS256 with the public key as the secret (the classic algorithm confusion)
    # (built by hand: PyJWT refuses to make one)
    public = tokens.KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=")
    signed = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": tokens.KID}).encode()) + b"." + b64(json.dumps(body).encode())
    confused = (signed + b"." + b64(hmac.new(public, signed, hashlib.sha256).digest())).decode()
    assert client.get("/admin/reports", headers={access.HEADER: confused}).status_code == 404
    assert client.get("/admin/reports", headers={access.HEADER: "not a token"}).status_code == 404


def test_not_configured_means_no_admin():
    verifier = access.Verifier(team_domain="", aud="", jwks_file="")
    assert verifier.admin({access.HEADER: tokens.token()}) is None


def test_admin_home_goes_to_the_reports(client):
    r = client.get("/admin", headers=tokens.headers())
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/reports")


def test_report_list(client, report_ids):
    r = client.get("/admin/reports", headers=tokens.headers())
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store" and "noindex" in r.headers["X-Robots-Tag"]
    page = r.get_data(as_text=True)
    assert tokens.EMAIL in page
    # to review by default: the accepted one isn't there
    assert {report_ids[0], report_ids[2]} <= shown(page) and report_ids[1] not in shown(page)
    assert "Línea de prueba &lt;b&gt;uno&lt;/b&gt;" in page       # visitors' text is escaped
    assert "Está mal cortado" in page and "inicio −0.3 s" in page and "0:04.7 – 0:07.0" in page
    assert "/clip/cancion_de_prueba/5000-7000.mp3" in page and "/clip/cancion_de_prueba/4700-7000.mp3" in page
    assert "Termina tarde" in page                                 # a report from before the single option


def test_report_list_all_and_by_song(client, report_ids):
    page = client.get("/admin/reports?show=all", headers=tokens.headers()).get_data(as_text=True)
    assert set(report_ids) <= shown(page) and "Ajuste aplicado" in page
    page = client.get("/admin/reports?show=all&song=otra_cancion", headers=tokens.headers()).get_data(as_text=True)
    assert report_ids[2] in shown(page) and not {report_ids[0], report_ids[1]} & shown(page)
    assert "Otra Cancion" in page


# --- decisions (phase 2): recorded on the site, "apply" and "delete" change the search at once

@pytest.fixture(autouse=True)
def nothing_applied_after():
    yield
    app_module.reports.set_status([r["id"] for r in app_module.reports.applied()], "revoked")
    app_module._index["checked"] = 0.0
    app_module._auto_applied.clear()


def report(client, **fields):
    body = {"song": "cancion_de_prueba", "start_ms": 9300, "end_ms": 10100, "problem": "timing"}
    body.update(fields)
    r = client.post("/report", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 201
    return r.get_json()["id"]


def decide(client, report_id, decision, note=None, headers=None, **kwargs):
    headers = {**tokens.headers(), "Origin": "http://localhost", **(headers or {})}
    return client.post(f"/admin/reports/{report_id}/decision", headers=headers,
                       **(kwargs or {"json": {"decision": decision, "note": note}}))


def search_page(client, query="sale+sobre"):
    return client.get("/search?lyric=" + query).get_data(as_text=True)


def test_decisions_need_an_admin_and_our_own_page(client):
    rid = report(client, problem="wrong_phrase")
    r = client.post(f"/admin/reports/{rid}/decision", json={"decision": "nothing"})
    assert r.status_code == 404                                              # no token: no admin pages
    assert decide(client, rid, None, data="decision=nothing",
                  content_type="application/x-www-form-urlencoded").status_code == 403    # a form from anywhere
    assert decide(client, rid, "nothing", headers={"Origin": "https://evil.example"}).status_code == 403
    assert decide(client, rid, "nothing", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert app_module.reports.get(rid)["decision"] is None


def test_impossible_decisions_are_refused(client):
    rid = report(client, problem="wrong_phrase")                             # no adjustment
    assert decide(client, rid, "apply").status_code == 400
    assert decide(client, rid, "made_up").status_code == 400
    assert decide(client, 999999, "nothing").status_code == 404


def test_apply_is_live_even_where_reports_wait_for_a_review(client, monkeypatch):
    monkeypatch.setattr(app_module, "AUTO_APPLY", False)                     # as in production
    rid = report(client, fixed_start_ms=9300, fixed_end_ms=10400, line="Sale sobre")
    assert "/clip/cancion_de_prueba/9300-10100.mp3" in search_page(client)    # waiting for a review
    before = REGISTRY.get_sample_value("extractos_report_reviews_total", {"decision": "apply"})
    r = decide(client, rid, "apply", note="se oye bien")
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert "/clip/cancion_de_prueba/9300-10400.mp3" in search_page(client)    # at once
    stored = app_module.reports.get(rid)
    assert (stored["decision"], stored["note"], stored["reviewed_by"]) == ("apply", "se oye bien", tokens.EMAIL)
    assert REGISTRY.get_sample_value("extractos_report_reviews_total", {"decision": "apply"}) == before + 1
    # reviewed: out of "Por revisar", with the decision shown in "Todos"
    assert rid not in shown(client.get("/admin/reports", headers=tokens.headers()).get_data(as_text=True))
    page = client.get("/admin/reports?show=all", headers=tokens.headers()).get_data(as_text=True)
    assert "Aplicar el ajuste" in page and "se oye bien" in page


def test_delete_removes_the_words_from_the_search(client):
    rid = report(client, problem="wrong_phrase")
    assert "/clip/cancion_de_prueba/9300-10100.mp3" in search_page(client)
    assert decide(client, rid, "delete").get_json()["status"] == "deleted"
    assert "/clip/cancion_de_prueba/9300-10100.mp3" not in search_page(client)


def test_other_decisions_take_back_a_provisional_fix(client):
    rid = report(client, fixed_start_ms=9300, fixed_end_ms=10400)            # applied at once (staging)
    assert "/clip/cancion_de_prueba/9300-10400.mp3" in search_page(client)
    assert decide(client, rid, "nothing").get_json()["status"] == "revoked"
    assert "/clip/cancion_de_prueba/9300-10100.mp3" in search_page(client)
