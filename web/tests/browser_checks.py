"""Browser tests of the page's JavaScript (Playwright: Chromium, Firefox and WebKit), on the made-up data.

Not collected with the unit tests (the name matches neither test_*.py nor *_test.py): run explicitly,
    cd web && python -m pytest tests/browser_checks.py
Needs `pip install playwright` + `playwright install chromium firefox webkit`, and ffmpeg for
real audio (conftest.py makes it); without ffmpeg the clip, download and playback checks are skipped.
Every test runs in each engine; BROWSERS="chromium" (space-separated) runs fewer.

Starts the app with gunicorn on the fixture data (conftest.py's environment), then drives
the page: suggestions, the "¿No coincide?" panel (waveform editor: edges by mouse, tap and keyboard, colours, voice preview, fallback to the song; preview, Descargar following the
adjustment, sending a report, reset), Resaltada | Original, other occurrences, the recent clips list, the play button, phone width,
JavaScript errors.
"""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import expect, sync_playwright

from conftest import HAS_FFMPEG

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# WebKit = Safari's engine, Firefox = Gecko: the play button once vanished in Chrome only
ENGINES = os.environ.get("BROWSERS", "chromium firefox webkit").split()


def as_visitor(target, browser, base_url):
    """Each engine is its own visitor (the header Cloudflare sets), so running all three
    doesn't hit the app's per-visitor limits (30 searches a minute). Only on requests to
    the app: the Tailwind CDN, behind Cloudflare itself, refuses that header."""
    ip = f"10.0.0.{ENGINES.index(browser.browser_type.name) + 1}"
    target.route(base_url + "/**", lambda route: route.continue_(
        headers={**route.request.headers, "CF-Connecting-IP": ip}))
    return target


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "gunicorn", "--bind", f"127.0.0.1:{port}", "--workers", "1",
                             "--threads", "8", "app:app"], cwd=WEB, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            env={**os.environ, "REPORTS_RECHECK_S": "0",     # a revoked report is gone at once
                                 "ORCHARDRUN_API_KEY": "test-key"})          # mic + guestbook buttons shown (faked per test)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail("app did not start: " + proc.stderr.read().decode()[-2000:])
            time.sleep(0.25)
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module", params=ENGINES)
def browser(request):
    with sync_playwright() as p:
        b = getattr(p, request.param).launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, base_url):
    ctx = as_visitor(browser.new_context(accept_downloads=True), browser, base_url)
    page = ctx.new_page()
    page.js_errors = []
    page.on("pageerror", lambda e: page.js_errors.append(str(e)))
    yield page
    assert page.js_errors == [], "JavaScript errors on the page"
    ctx.close()


def download_href(card):
    return card.locator(".download").get_attribute("href").split("?")[0]


def mp3_seconds(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-", "-stats"],
                         capture_output=True, text=True).stderr
    h, m, s = out.strip().split("time=")[-1].split()[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def stored_reports():
    with sqlite3.connect(os.environ["REPORTS_DB"]) as conn:
        return conn.execute("SELECT song, start_ms, end_ms, problem, fixed_start_ms, fixed_end_ms FROM reports"
                            " ORDER BY id").fetchall()


# --- search box

def test_suggestions_complete_a_line(page, base_url):
    page.goto(base_url + "/")
    page.locator("#lyric").press_sequentially("vamos a", delay=30)
    suggestion = page.locator("#suggestions li").first
    suggestion.wait_for()
    assert "Vamos a brillar esta noche" in suggestion.inner_text()
    suggestion.click()
    page.wait_for_url("**/search?lyric=*")
    assert page.locator(".audio-card").count() >= 1


# A microphone and recorder that "record" a few bytes at once: no device or permission prompt needed
FAKE_RECORDER = """
// the whole object: Playwright's WebKit may have none (or ignore assigning to it)
Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } });
window.MediaRecorder = class extends EventTarget {
    constructor() { super(); this.state = 'inactive'; this.mimeType = 'audio/webm;codecs=opus'; }
    start() { this.state = 'recording'; }
    stop() {
        this.state = 'inactive';
        const e = new Event('dataavailable'); e.data = new Blob(['fake opus'], { type: this.mimeType });
        this.dispatchEvent(e); this.dispatchEvent(new Event('stop'));
    }
};
"""


def test_mic_searches_what_was_said(page, base_url):
    sent = []

    def transcribe(route):
        sent.append(route.request.headers.get("content-type", ""))
        route.fulfill(json={"text": "sale sobre"})
    page.add_init_script(FAKE_RECORDER)
    page.route(base_url + "/transcribe", transcribe)
    page.goto(base_url + "/")
    mic = page.locator("#mic")
    mic.click()
    expect(mic).to_have_attribute("data-state", "recording")
    mic.click()
    page.wait_for_url("**/search?lyric=sale+sobre")
    assert sent and sent[0].startswith("multipart/form-data")
    assert page.locator(".audio-card").count() >= 1


def test_mic_says_when_nothing_was_heard(page, base_url):
    page.add_init_script(FAKE_RECORDER)
    page.route(base_url + "/transcribe", lambda route: route.fulfill(json={"text": ""}))
    page.goto(base_url + "/")
    page.locator("#mic").click()
    page.locator("#mic").click()
    expect(page.locator("#mic-status")).to_contain_text("No se escuchó nada")
    expect(page.locator("#mic")).to_have_attribute("data-state", "idle")


def test_mic_hides_when_out_of_credit(page, base_url):
    page.add_init_script(FAKE_RECORDER)
    page.route(base_url + "/transcribe", lambda route: route.fulfill(status=503, json={"error": "no_credit"}))
    page.goto(base_url + "/")
    page.locator("#mic").click()
    page.locator("#mic").click()
    expect(page.locator("#mic-status")).to_contain_text("no está disponible")
    expect(page.locator("#mic")).to_be_hidden()


# --- Libro de visitas (the green button: hold, release to send, slide left to cancel, up to lock)

def guestbook_page(page, base_url, answer=None):
    """The page scrolled to the guestbook, the recorder and /guestbook faked; returns the posts sent."""
    sent = []

    def post(route):
        sent.append(route.request.headers.get("content-type", ""))
        route.fulfill(**(answer or {"status": 201, "json": {"id": 1, "text": "Hola, un saludo"}}))
    page.add_init_script(FAKE_RECORDER)
    page.route(base_url + "/guestbook", post)
    page.goto(base_url + "/")
    page.locator('a[href="#mensajes"]').click()
    expect(page.locator("body")).to_have_class(re.compile("in-guestbook"))      # the background changed
    mic = page.locator("#mensajes .gb-mic")
    box = mic.bounding_box()
    return sent, mic, (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def test_guestbook_hold_and_release_sends(page, base_url):
    sent, mic, (x, y) = guestbook_page(page, base_url)
    section = page.locator("#mensajes")
    page.mouse.move(x, y)
    page.mouse.down()
    expect(section).to_have_attribute("data-state", "recording")
    expect(section.locator(".gb-slide")).to_be_visible()
    page.wait_for_timeout(1100)
    page.mouse.up()
    expect(section.locator(".gb-out")).to_contain_text("Hola, un saludo")
    expect(section.locator(".gb-out .gb-meta")).to_have_text("Enviado ✓✓")
    expect(section).to_have_attribute("data-state", "idle")
    assert sent and sent[0].startswith("multipart/form-data")


def test_guestbook_short_press_slide_to_cancel_and_lock(page, base_url):
    sent, mic, (x, y) = guestbook_page(page, base_url)
    section = page.locator("#mensajes")
    page.mouse.move(x, y)                  # a tap: too short, explains how
    page.mouse.down()
    page.mouse.up()
    expect(section.locator(".gb-status")).to_contain_text("Mantené apretado")
    page.mouse.down()                      # slid left: cancelled
    page.wait_for_timeout(1100)
    page.mouse.move(x - 120, y, steps=5)
    expect(section.locator(".gb-status")).to_contain_text("descartado")
    page.mouse.up()
    page.mouse.move(x, y)                  # slid up: locked, the button sends
    page.mouse.down()
    page.mouse.move(x, y - 100, steps=5)
    expect(section).to_have_attribute("data-state", "locked")
    page.mouse.up()
    expect(section).to_have_attribute("data-state", "locked")      # letting go doesn't send
    expect(section.locator(".gb-trash")).to_be_visible()
    page.wait_for_timeout(1100)
    assert sent == []
    mic.click()
    expect(section.locator(".gb-out")).to_contain_text("Hola, un saludo")
    assert len(sent) == 1


def test_guestbook_keyboard_and_out_of_credit(page, base_url):
    sent, mic, _ = guestbook_page(page, base_url, {"status": 503, "json": {"error": "no_credit"}})
    section = page.locator("#mensajes")
    mic.focus()
    page.keyboard.press("Enter")
    expect(section).to_have_attribute("data-state", "locked")
    page.wait_for_timeout(1100)
    page.keyboard.press("Enter")
    expect(section.locator(".gb-status")).to_contain_text("no se pueden dejar mensajes")
    expect(section.locator(".gb-composer")).to_be_hidden()
    expect(section.locator(".gb-out")).to_have_count(0)


# --- ¿No coincide?

def test_report_panel_validation(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    assert card.locator(".report-panel").is_hidden()
    card.get_by_text("¿No coincide?").click()
    assert card.locator(".report-panel").is_visible()
    assert card.locator(".report-start").inner_text() == "0:09.3"     # the fixture's timing for "sale"
    card.get_by_role("button", name="Enviar").click()
    assert "Elegí" in card.locator(".report-status").inner_text()     # no problem chosen: nothing sent


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_adjusting_moves_preview_and_download(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    served = download_href(card)
    assert served.endswith("/9300-10100.mp3")
    card.get_by_role("button", name="Original").click()                 # the song: the edges + its 0.3 s fade-in
    card.get_by_text("¿No coincide?").click()
    start, end = card.locator(".wave-handle[data-edge=start]"), card.locator(".wave-handle[data-edge=end]")
    start.press("ArrowLeft")                                            # keyboard: 0.1 s
    assert card.locator(".report-start").inner_text() == "0:09.2"
    start.press("ArrowLeft")
    end.press("Shift+ArrowRight")                                       # with Shift: 1 s
    assert (card.locator(".report-start").inner_text(), card.locator(".report-end").inner_text()) == ("0:09.1", "0:11.1")
    assert download_href(card).endswith("/9100-11100.mp3")            # Descargar follows the edges
    with page.expect_download() as d:
        card.locator(".download").click()
    # exactly the 2.0 s adjusted + the 0.3 s fade-in (mp3 frames: a few hundredths more or less)
    assert 2.2 <= mp3_seconds(d.value.path()) <= 2.4
    card.get_by_text("▶ Escuchar el ajuste").click()
    assert card.locator("audio").get_attribute("src").endswith("/9100-11100.mp3")


def test_sending_a_report(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    card.get_by_text("¿No coincide?").click()
    card.locator(".wave-handle[data-edge=start]").press("Shift+ArrowLeft")
    card.get_by_label("Está mal cortado (empieza o termina antes o después)").check()
    before = len(stored_reports())
    with page.expect_response("**/report") as r:
        card.get_by_role("button", name="Enviar").click()
    assert r.value.status == 201
    assert card.locator(".report-thanks").is_visible()
    assert card.locator(".report-panel").is_hidden() and card.locator(".report-toggle").is_hidden()
    rows = stored_reports()
    assert len(rows) == before + 1
    # the clip as served, with the visitor's correction
    assert rows[-1] == ("cancion_de_prueba", 9300, 10100, "timing", 8300, 10100)
    # applied at once (provisional): searching again gives the corrected clip
    page.goto(base_url + "/search?lyric=sale+sobre")
    assert download_href(page.locator(".audio-card").first).endswith("/8300-10100.mp3")
    # until a review revokes it (the other tests, and engines, expect the fixture's timing)
    with sqlite3.connect(os.environ["REPORTS_DB"]) as conn:
        conn.execute("UPDATE reports SET status = 'revoked' WHERE status = 'provisional'")
    page.goto(base_url + "/search?lyric=sale+sobre")
    assert download_href(page.locator(".audio-card").first).endswith("/9300-10100.mp3")


def test_reopening_goes_back_to_the_served_clip(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    toggle = card.get_by_text("¿No coincide?")
    toggle.click()
    card.locator(".wave-handle[data-edge=start]").press("Shift+ArrowRight")
    # +1 s from 9.3 s would pass the end (10.1 s): capped at 0.2 s before it
    assert download_href(card).endswith("/9900-10100.mp3")
    toggle.click()                                                      # close without sending
    toggle.click()                                                      # and open again
    assert card.locator(".report-start").inner_text() == "0:09.3"
    assert download_href(card).endswith("/9300-10100.mp3")


def test_other_occurrence_resets_the_panel(page, base_url):
    page.goto(base_url + "/search?lyric=vamos+a+brillar+esta+noche")   # sung twice in the fixture
    card = page.locator(".audio-card").first
    chips = card.locator(".occurrence")
    assert chips.count() == 2
    card.get_by_text("¿No coincide?").click()
    first = card.locator(".report-start").inner_text()
    chips.nth(1).click()
    assert card.locator(".report-start").inner_text() != first          # now about the second time
    # Descargar: that time as Resaltada (the default with both stems)
    assert download_href(card) == chips.nth(1).get_attribute("data-clip").replace("/clip/", "/mix/")



# --- the timing editor's waveform (¿No coincide?)

def wave_pixel(card, at):
    """RGB of the waveform canvas at a fraction of its width, on the centre line."""
    return card.locator(".wave-canvas").evaluate("""(c, at) => {
        const x = Math.floor(c.width * at), y = Math.floor(c.height / 2);
        return Array.from(c.getContext('2d').getImageData(x, y, 1, 1).data.slice(0, 3));
    }""", at)


def open_editor(page, base_url, query):
    page.goto(base_url + "/search?lyric=" + query)
    card = page.locator(".audio-card").first
    card.get_by_text("¿No coincide?").click()
    expect(card.locator(".wave")).to_have_attribute("data-state", "ready")
    return card


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_waveform_inside_coloured_outside_grey(page, base_url):
    card = open_editor(page, base_url, "sale+sobre")
    assert card.locator(".wave-source").inner_text() == "solo la voz"          # the vocal stem
    # window 4.3–15.1 s, selection 9.3–10.1 s: its middle is at 0.537 of the width
    assert wave_pixel(card, 0.537) == [0x74, 0xAC, 0xDF]
    assert wave_pixel(card, 0.2) == [0xD1, 0xD5, 0xDB]
    assert wave_pixel(card, 0.9) == [0xD1, 0xD5, 0xDB]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_dragging_an_edge_and_tapping_the_waveform(page, base_url):
    card = open_editor(page, base_url, "sale+sobre")
    card.locator(".wave").scroll_into_view_if_needed()                # the mouse works in the viewport
    box = card.locator(".wave").bounding_box()
    x_at = lambda ms: box["x"] + box["width"] * (ms - 4300) / 10800
    y = box["y"] + box["height"] / 2
    # drag the start edge from 9.3 s to about 7.3 s
    page.mouse.move(x_at(9300), y)
    page.mouse.down()
    page.mouse.move(x_at(8300), y, steps=5)
    page.mouse.move(x_at(7300), y, steps=5)
    page.mouse.up()
    start_ms = int(re.search(r"/(\d+)-10100\.mp3$", download_href(card))[1])
    assert abs(start_ms - 7300) <= 30
    assert wave_pixel(card, (8300 - 4300) / 10800) == [0x74, 0xAC, 0xDF]   # now inside
    # a tap past the end brings the end edge there (nearest edge)
    page.mouse.click(x_at(12000), y)
    end_ms = int(re.search(r"-(\d+)\.mp3$", download_href(card))[1])
    assert abs(end_ms - 12000) <= 30


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_voice_preview_plays_the_selection(page, base_url):
    card = open_editor(page, base_url, "sale+sobre")
    button = card.locator(".voice-play")
    button.click()
    expect(button).to_have_text("■ Parar")
    expect(card.locator(".wave-playhead")).to_be_visible()
    expect(button).to_have_text("▶ Escuchar la voz", timeout=5000)             # 0.8 s selection, then stops
    expect(card.locator(".wave-playhead")).to_be_hidden()


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_song_without_vocal_stem_shows_the_song(page, base_url):
    card = open_editor(page, base_url, "tren+llega+tarde")
    assert card.locator(".wave-source").inner_text() == "la canción (no hay voz separada)"

# --- Tus últimos audios (static/recent.js)

# The system's share sheet: accepts (or, with window.refuse, is closed)
FAKE_SHARE = """
Object.defineProperty(navigator, 'canShare', { value: () => true });
Object.defineProperty(navigator, 'share', { value: async () => {
    if (window.refuse) throw new DOMException('closed', 'AbortError');
} });
"""


def recent_entry(n, song="cancion_de_prueba"):
    return {"clip": f"/clip/{song}/{5000 + n}-7000.mp3", "listen": "original", "song": "Cancion De Prueba",
            "phrase": f"frase {n}", "filename": f"f{n}.mp3", "at": n}


def store_recent(page, base_url, value):
    page.goto(base_url + "/")
    page.evaluate("v => localStorage.setItem('unaudio.recent', v)", value)
    page.reload()


def test_nothing_recent_at_first(page, base_url):
    page.goto(base_url + "/")
    expect(page.locator("#recent")).to_be_hidden()


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_downloads_are_listed_on_the_landing_page(page, base_url):
    page.goto(base_url + "/search?lyric=nadie duerme")
    card = page.locator(".audio-card").first
    assert page.locator("#recent").count() == 0
    for version in ("mix", "original", "mix"):              # the second mix: to the top, no duplicate
        card.locator(f".listen [data-listen={version}]").click()
        with page.expect_download():
            card.locator(".download").click()
    page.goto(base_url + "/")
    items = page.locator(".recent-item")
    expect(items).to_have_count(2)
    expect(items.locator(".recent-version")).to_have_text(["Resaltada", "Original"])
    expect(items.first.locator(".recent-phrase")).to_have_text("Nadie duerme")
    expect(items.first.locator("h3")).to_have_text("Cancion De Prueba")
    with page.expect_download() as d:
        items.first.locator(".download").click()
    assert d.value.suggested_filename == "Cancion De Prueba - Nadie duerme.mp3"
    items.first.locator(".play-btn").click()
    expect(items.first.locator(".play-btn")).to_have_attribute("data-state", "playing")
    items.first.locator(".recent-phrase").click()
    page.wait_for_url("**/search?lyric=Nadie%20duerme")


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_only_completed_shares_are_listed(page, base_url):
    page.add_init_script(FAKE_SHARE)
    page.goto(base_url + "/search?lyric=nadie duerme")
    share = page.locator(".share").first
    page.evaluate("window.refuse = true")
    with page.expect_request("**/event"):
        share.click()
    assert page.evaluate("localStorage.getItem('unaudio.recent')") is None
    page.evaluate("window.refuse = false")
    with page.expect_request("**/event"):
        share.click()
    page.goto(base_url + "/")
    expect(page.locator(".recent-item")).to_have_count(1)
    with page.expect_request("**/event"):
        page.locator(".recent-item .share").click()


def test_recent_list_more_remove_and_clear(page, base_url):
    fetched = []
    page.add_init_script(FAKE_SHARE)
    page.route("**/original/**", lambda r: (fetched.append(r.request.url), r.fulfill(body=b"x", content_type="audio/mpeg")))
    store_recent(page, base_url, json.dumps([recent_entry(n) for n in range(25)]))
    visible = page.locator(".recent-item:visible")
    expect(visible).to_have_count(5)
    page.wait_for_load_state("networkidle")
    assert len({u.split("?")[0] for u in fetched}) == 5     # only what's shown is fetched for Compartir
    visible.first.locator(".recent-remove").click()
    expect(visible).to_have_count(5)
    expect(visible.first.locator(".recent-phrase")).to_have_text("frase 1")
    page.locator(".recent-more").click()
    expect(visible).to_have_count(19)
    expect(page.locator(".recent-more")).to_be_hidden()
    page.reload()
    expect(visible.first.locator(".recent-phrase")).to_have_text("frase 1")
    page.once("dialog", lambda d: d.accept())
    page.locator(".recent-clear").click()
    expect(page.locator("#recent")).to_be_hidden()
    page.reload()
    expect(page.locator("#recent")).to_be_hidden()


def test_recent_ignores_damaged_storage(page, base_url):
    store_recent(page, base_url, "not json")
    expect(page.locator("#recent")).to_be_hidden()
    bad = {**recent_entry(1), "clip": "javascript:alert(1)"}
    store_recent(page, base_url, json.dumps([None, 3, bad, {**recent_entry(2), "phrase": "<b>x</b>"}]))
    expect(page.locator(".recent-phrase")).to_have_text(["<b>x</b>"])       # text, not HTML


def test_recent_clip_no_longer_there(page, base_url):
    store_recent(page, base_url, json.dumps([recent_entry(1, song="cancion_borrada")]))
    page.locator(".recent-item .play-btn").click()
    expect(page.locator(".recent-gone")).to_be_visible()
    expect(page.locator(".recent-actions")).to_be_hidden()


def test_recent_list_fits_a_phone(page, base_url):
    page.set_viewport_size({"width": 360, "height": 740})
    page.add_init_script(FAKE_SHARE)
    store_recent(page, base_url, json.dumps([{**recent_entry(n), "phrase": "una frase bastante larga " * 4} for n in range(7)]))
    expect(page.locator(".recent-item:visible")).to_have_count(5)
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
    if os.environ.get("SHOTS"):
        page.screenshot(path=os.path.join(os.environ["SHOTS"], "phone.png"))


# --- Resaltada | Original (what plays, downloads and is shared)

@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_resaltada_by_default_and_the_original(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    audio, play = card.locator("audio"), card.locator(".play-btn")
    mix, original = card.get_by_role("button", name="Resaltada"), card.get_by_role("button", name="Original")
    expect(mix).to_have_attribute("aria-pressed", "true")
    assert audio.get_attribute("src") == "/mix/cancion_de_prueba/9300-10100.mp3"
    assert download_href(card) == "/mix/cancion_de_prueba/9300-10100.mp3"
    play.click()
    expect(play).to_have_attribute("data-state", "idle", timeout=5000)       # 1.8 s, played to the end
    assert audio.evaluate("a => a.error") is None
    with page.expect_download() as d:
        card.locator(".download").click()
    assert 1.75 <= mp3_seconds(d.value.path()) <= 1.9                 # 1 s lead + the 0.8 s phrase
    original.click()                                                    # player and Descargar switch
    expect(original).to_have_attribute("aria-pressed", "true")
    assert audio.get_attribute("src") == "/original/cancion_de_prueba/9300-10100.mp3"
    assert download_href(card) == "/original/cancion_de_prueba/9300-10100.mp3"
    # an adjustment in ¿No coincide? plays in the chosen version
    mix.click()
    card.get_by_text("¿No coincide?").click()
    card.locator(".wave-handle[data-edge=start]").press("Shift+ArrowLeft")
    card.get_by_text("▶ Escuchar el ajuste").click()
    assert audio.get_attribute("src") == "/mix/cancion_de_prueba/8300-10100.mp3"


def test_no_switch_without_stems(page, base_url):
    page.goto(base_url + "/search?lyric=tren+llega+tarde")
    card = page.locator(".audio-card").first
    assert card.locator(".listen").count() == 0
    assert download_href(card).startswith("/original/") and card.locator("audio").get_attribute("src").startswith("/original/")


# --- play button

@pytest.mark.parametrize("width", [1280, 375])
def test_play_button_is_visible(browser, base_url, width):
    page = as_visitor(browser.new_page(viewport={"width": width, "height": 800}), browser, base_url)
    page.goto(base_url + "/search?lyric=sale+sobre")
    box = page.locator(".play-btn").first.bounding_box()
    # <audio controls> used to be 0 px wide in Chrome at desktop width
    assert box and box["width"] >= 100 and box["height"] >= 40
    page.close()


def state(card):
    return expect(card.locator(".play-btn"))


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_play_button_plays_pauses_and_stops_the_others(page, base_url):
    page.goto(base_url + "/search?lyric=brillar")                       # a line in each fixture song
    cards = page.locator(".audio-card")
    assert cards.count() >= 2
    first, second = cards.nth(0), cards.nth(1)
    page.evaluate("document.querySelectorAll('audio').forEach(a => a.loop = true)")   # clips last ~1 s
    first.locator(".play-btn").click()
    state(first).to_have_attribute("data-state", "playing")
    expect(first.locator(".play-label")).to_have_text("❚❚ Pausa")
    state(first).to_have_attribute("aria-label", re.compile("^Pausar: "))
    second.locator(".play-btn").click()                                 # one clip at a time
    state(second).to_have_attribute("data-state", "playing")
    state(first).to_have_attribute("data-state", "idle")
    assert first.locator("audio").evaluate("a => a.paused")
    second.locator(".play-btn").click()                                 # second tap pauses
    state(second).to_have_attribute("data-state", "idle")
    expect(second.locator(".play-label")).to_have_text("▶ Escuchar")
    assert second.locator("audio").evaluate("a => a.paused")


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg for real audio")
def test_other_occurrence_plays_through_the_button(page, base_url):
    page.goto(base_url + "/search?lyric=vamos+a+brillar+esta+noche")
    card = page.locator(".audio-card").first
    page.evaluate("document.querySelectorAll('audio').forEach(a => a.loop = true)")
    card.locator(".occurrence").nth(1).click()
    state(card).to_have_attribute("data-state", "playing")              # the button shows plays started elsewhere


def test_play_button_offers_a_retry_when_the_clip_fails(page, base_url):
    clips = re.compile(r"/(clip|mix|original)/.*\.mp3")                # Resaltada /mix/, Original /original/
    page.route(clips, lambda route: route.fulfill(status=503, body="busy"))
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    card.locator(".play-btn").click()
    state(card).to_have_attribute("data-state", "error")
    expect(card.locator(".play-label")).to_have_text("↻ Reintentar")
    if HAS_FFMPEG:                                                      # the retry downloads it again
        page.unroute(clips)
        card.locator("audio").evaluate("a => a.loop = true")
        card.locator(".play-btn").click()
        state(card).to_have_attribute("data-state", "playing")


def test_phone_width_has_no_sideways_scroll(browser, base_url):
    page = as_visitor(browser.new_page(viewport={"width": 375, "height": 800}), browser, base_url)
    page.goto(base_url + "/search?lyric=sale+sobre")
    page.locator(".report-toggle").first.click()
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    page.close()


# --- admin (behind Cloudflare Access: here a token signed by the tests' key, tokens.py)

def test_admin_records_a_decision(page, base_url):
    import json
    import tokens
    body = json.dumps({"song": "otra_cancion", "start_ms": 1000, "end_ms": 2500, "problem": "wrong_phrase",
                       "line": "Línea para revisar"}).encode()
    request = urllib.request.Request(base_url + "/report", data=body, headers={"Content-Type": "application/json"})
    report_id = json.load(urllib.request.urlopen(request))["id"]
    assert page.goto(base_url + "/admin/reports").status == 404                # no token: no page
    page.set_extra_http_headers(tokens.headers())
    page.goto(base_url + "/admin/reports")
    card = page.locator(f'.report[data-id="{report_id}"]')
    expect(card).to_contain_text("Línea para revisar")
    card.locator(".note").fill("no es esta frase, es la siguiente")
    card.get_by_role("button", name="Está bien así").click()                  # the browser's own fetch
    expect(card.locator(".status")).to_have_text("Revisado")
    expect(card.locator(".reviewed")).to_contain_text("Está bien así")
    with sqlite3.connect(os.environ["REPORTS_DB"]) as conn:
        assert conn.execute("SELECT decision, note, status FROM reports WHERE id = ?", (report_id,)).fetchone() == \
            ("nothing", "no es esta frase, es la siguiente", "revoked")


def test_admin_sets_its_own_times_on_the_waveform(page, base_url):
    import json
    import tokens
    body = json.dumps({"song": "cancion_de_prueba", "start_ms": 9300, "end_ms": 10100, "problem": "wrong_phrase"}).encode()
    request = urllib.request.Request(base_url + "/report", data=body, headers={"Content-Type": "application/json"})
    report_id = json.load(urllib.request.urlopen(request))["id"]
    page.set_extra_http_headers(tokens.headers())
    page.goto(base_url + "/admin/reports")
    card = page.locator(f'.report[data-id="{report_id}"]')
    apply = card.get_by_role("button", name="Aplicar el ajuste")
    expect(apply).to_be_disabled()                                           # no adjustment yet
    card.locator(".tune summary").click()
    expect(card.locator(".wave")).to_have_attribute("data-state", "ready")
    assert card.locator(".report-start").inner_text() == "0:09.3"
    card.locator(".wave-handle[data-edge=start]").press("ArrowLeft")
    card.locator(".wave-handle[data-edge=end]").press("Shift+ArrowRight")
    assert (card.locator(".report-start").inner_text(), card.locator(".report-end").inner_text()) == ("0:09.2", "0:11.1")
    expect(apply).to_be_enabled()
    apply.click()
    expect(card.locator(".status")).to_have_text("Ajuste aplicado")
    with sqlite3.connect(os.environ["REPORTS_DB"]) as conn:
        assert conn.execute("SELECT decision, status, review_start_ms, review_end_ms FROM reports WHERE id = ?",
                            (report_id,)).fetchone() == ("apply", "accepted", 9200, 11100)
    page.goto(base_url + "/search?lyric=sale+sobre")
    assert page.locator('.audio-card[data-served*="/9200-11100.mp3"]').count() == 1    # live in the search
    # take it back (the next engine's report needs the phrase where the fixture has it)
    page.goto(base_url + "/admin/reports")
    assert page.evaluate("""id => fetch(`/admin/reports/${id}/decision`, {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision: 'nothing'})}).then(r => r.status)""",
        report_id) == 200
