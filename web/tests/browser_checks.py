"""Browser tests of the page's JavaScript (Chromium via Playwright), on the made-up data.

Not collected with the unit tests (the name matches neither test_*.py nor *_test.py): run explicitly,
    cd web && python -m pytest tests/browser_checks.py
Needs `pip install playwright` + `playwright install chromium`, and ffmpeg for real audio
(conftest.py makes it); without ffmpeg the clip and download checks are skipped.

Starts the app with gunicorn on the fixture data (conftest.py's environment), then drives
the page: suggestions, the "¿No coincide?" panel (nudges, preview, Descargar following the
adjustment, sending a report, reset), other occurrences, phone width, JavaScript errors.
"""

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from conftest import HAS_FFMPEG

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "gunicorn", "--bind", f"127.0.0.1:{port}", "--workers", "1",
                             "--threads", "8", "app:app"], cwd=WEB, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(accept_downloads=True)
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
    card.get_by_text("¿No coincide?").click()
    card.locator("[data-edge=start][data-delta='-200']").click()
    card.locator("[data-edge=end][data-delta='1000']").click()
    assert (card.locator(".report-start").inner_text(), card.locator(".report-end").inner_text()) == ("0:09.1", "0:11.1")
    assert download_href(card).endswith("/9100-11100.mp3")            # Descargar follows the nudges
    with page.expect_download() as d:
        card.locator(".download").click()
    # 2.0 s adjusted + 0.2 s before + 0.3 s after
    assert 2.3 <= mp3_seconds(d.value.path()) <= 2.7
    card.get_by_text("▶ Escuchar el ajuste").click()
    assert card.locator("audio").get_attribute("src").endswith("/9100-11100.mp3")


def test_sending_a_report(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    card.get_by_text("¿No coincide?").click()
    card.locator("[data-edge=start][data-delta='-1000']").click()
    card.get_by_label("Termina tarde (se escucha otra cosa al final)").check()
    before = len(stored_reports())
    with page.expect_response("**/report") as r:
        card.get_by_role("button", name="Enviar").click()
    assert r.value.status == 201
    assert card.locator(".report-thanks").is_visible()
    assert card.locator(".report-panel").is_hidden() and card.locator(".report-toggle").is_hidden()
    rows = stored_reports()
    assert len(rows) == before + 1
    # the clip as served, with the visitor's correction
    assert rows[-1] == ("cancion_de_prueba", 9300, 10100, "ends_late", 8300, 10100)


def test_reopening_goes_back_to_the_served_clip(page, base_url):
    page.goto(base_url + "/search?lyric=sale+sobre")
    card = page.locator(".audio-card").first
    toggle = card.get_by_text("¿No coincide?")
    toggle.click()
    card.locator("[data-edge=start][data-delta='1000']").click()
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
    assert download_href(card) == chips.nth(1).get_attribute("data-clip")


def test_phone_width_has_no_sideways_scroll(browser, base_url):
    page = browser.new_page(viewport={"width": 375, "height": 800})
    page.goto(base_url + "/search?lyric=sale+sobre")
    page.locator(".report-toggle").first.click()
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
    page.close()
