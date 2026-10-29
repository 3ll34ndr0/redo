"""Speech to text for the mic button: the visitor says (or sings) the phrase, Orchardrun's
transcription API (OpenAI-compatible /v1/audio/transcriptions) turns it into text to search.
Same service and settings as ~/Documentos/lectura. No key (ORCHARDRUN_API_KEY): no mic button."""

import json
import os
import urllib.error
import urllib.request
import uuid

URL = os.getenv("ORCHARDRUN_STT_URL", "https://api.orchardrun.com/v1/audio/transcriptions")
TIMEOUT_S = 30


class Unavailable(Exception):
    """The service can't be reached or refused the audio (the reason is in the message, for the log)."""


def enabled():
    return bool(os.getenv("ORCHARDRUN_API_KEY"))


def _multipart(fields, file_field, filename, content_type, data):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f'Content-Type: {content_type}\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def transcribe(audio, filename, content_type):
    """The text said in the recording ('' if nothing). Raises Unavailable."""
    # `language` is a hint for the model, not a constraint
    body, body_type = _multipart({'language': 'es', 'response_format': 'json'}, 'file', filename, content_type, audio)
    req = urllib.request.Request(URL, data=body, method='POST', headers={
        'Authorization': f"Bearer {os.environ['ORCHARDRUN_API_KEY']}", 'Content-Type': body_type,
        # Orchardrun's Cloudflare refuses urllib's default (Python-urllib/3.x): 403, error code 1010
        'User-Agent': 'unaudio (+https://unaudio.marso.ar)'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            answer = json.load(resp)
    except urllib.error.HTTPError as e:
        # kept for the log only: it may show account or billing details
        raise Unavailable(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise Unavailable(repr(e)) from None
    return (answer.get('text') or '').strip()
