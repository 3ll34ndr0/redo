"""The footer's sentence: one at random per page from a curated list.

The list is a plain text file, one sentence per line (# comments and blank lines skipped). It
comes from FOOTER_PHRASES_URL when that's set (in k8s: a raw file in the user's GitHub repo, so
it's edited there without a deploy), re-fetched every REFRESH_S in a background thread so a page
never waits for GitHub; until the first answer, and whenever a fetch fails or gives an empty
list, the last good list stays. The bundled web/footer_phrases.txt is the starting list.
"""

import os
import random
import threading
import time
import urllib.request

import observability as obs

URL = os.getenv("FOOTER_PHRASES_URL", "")
REFRESH_S = int(os.getenv("FOOTER_PHRASES_REFRESH_S", "300"))
TIMEOUT_S = 5
MAX_BYTES = 64 * 1024
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "footer_phrases.txt")


def parse(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class Phrases:
    def __init__(self, url=URL, refresh_s=REFRESH_S, bundled=BUNDLED):
        with open(bundled, encoding="utf-8") as f:
            self.phrases = parse(f.read())
        self.url, self.refresh_s = url, refresh_s
        self.checked = float("-inf")
        self.lock = threading.Lock()

    def fetch(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": "unaudio (+https://unaudio.marso.ar)"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as answer:
                phrases = parse(answer.read(MAX_BYTES).decode("utf-8"))
        except (OSError, ValueError) as e:            # URLError, HTTPError, timeouts, bad UTF-8
            obs.log("footer_phrases_failed", level="error", error=repr(e))
            return
        if not phrases:
            obs.log("footer_phrases_failed", level="error", error="empty list")
            return
        if phrases != self.phrases:
            self.phrases = phrases
            obs.log("footer_phrases_loaded", phrases=len(phrases))

    def refresh(self):
        """Start a background fetch when the list is due (at most one at a time)."""
        if not self.url:
            return
        with self.lock:
            if time.monotonic() - self.checked < self.refresh_s:
                return
            self.checked = time.monotonic()
        threading.Thread(target=self.fetch, daemon=True).start()

    def pick(self):
        self.refresh()
        return random.choice(self.phrases)
