"""Speech to text for the mic button: the visitor says (or sings) the phrase, Orchardrun's
transcription API (OpenAI-compatible /v1/audio/transcriptions) turns it into text to search.
Same service and settings as ~/Documentos/lectura. No key (ORCHARDRUN_API_KEY): no mic button.
Out of credit (HTTP 402): no mic button for NO_CREDIT_PAUSE_S; then one recording tries again.
Each feature names the env variable holding its key (key_env): the guestbook (guestbook.py) can
use a key of its own, with its own credit and its own pause."""

import json
import os
import time
import urllib.error
import urllib.request
import uuid

URL = os.getenv("ORCHARDRUN_STT_URL", "https://api.orchardrun.com/v1/audio/transcriptions")
TIMEOUT_S = 30
NO_CREDIT_PAUSE_S = 7 * 24 * 3600
KEY_ENV = "ORCHARDRUN_API_KEY"
_no_credit_until = {}          # key_env -> time.time() when its key may be tried again


class Unavailable(Exception):
    """The service can't be reached or refused the audio (the reason is in the message, for the log)."""


class NoCredit(Unavailable):
    """The account ran out of credit (HTTP 402): the mic button hides for a while."""


def configured(key_env=KEY_ENV):
    return bool(os.getenv(key_env))


def enabled(key_env=KEY_ENV):
    """The mic button is shown: a key, and no 402 in the last NO_CREDIT_PAUSE_S."""
    return configured(key_env) and time.time() >= _no_credit_until.get(key_env, 0.0)


def _multipart(fields, file_field, filename, content_type, data):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f'Content-Type: {content_type}\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def transcribe(audio, filename, content_type, key_env=KEY_ENV):
    """The text said in the recording ('' if nothing). Raises Unavailable."""
    # `language` is a hint for the model, not a constraint
    body, body_type = _multipart({'language': 'es', 'response_format': 'json'}, 'file', filename, content_type, audio)
    req = urllib.request.Request(URL, data=body, method='POST', headers={
        'Authorization': f"Bearer {os.environ[key_env]}", 'Content-Type': body_type,
        # Orchardrun's Cloudflare refuses urllib's default (Python-urllib/3.x): 403, error code 1010
        'User-Agent': 'unaudio (+https://unaudio.marso.ar)'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            answer = json.load(resp)
    except urllib.error.HTTPError as e:
        # kept for the log only: it may show account or billing details
        if e.code == 402:
            _no_credit_until[key_env] = time.time() + NO_CREDIT_PAUSE_S
            raise NoCredit(f"HTTP 402: {e.read()[:300].decode(errors='replace')}") from None
        raise Unavailable(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise Unavailable(repr(e)) from None
    return (answer.get('text') or '').strip()
