from flask import Flask, abort, jsonify, render_template, request, send_file, url_for
from flask_limiter import Limiter
from contextlib import closing
import sqlite3
import re
import subprocess
import tempfile
import threading
import time
import os

import observability as obs
from reports import PROBLEMS, ReportStore
from search import Index

app = Flask(__name__)

# Config
DB_PATH = os.getenv("DB_PATH", "redondos.db")
MUSIC_DIR = os.getenv("LIBRARY_PATH")
SNIPPET_DIR = os.getenv("SNIPPET_CACHE_DIR") or os.path.join(app.static_folder, "snippets")
MAX_RESULTS = 10
MAX_CLIP_S = 60
PAD_BEFORE, PAD_AFTER = 0.2, 0.3      # seconds of padding around the sung phrase
CACHE_MB = int(os.getenv("SNIPPET_CACHE_MB", "300"))   # oldest clips deleted above this
REPORTS_DB = os.getenv("REPORTS_DB", "reports.db")     # clip reports (writable; see reports.py)
CLIP_MAX_AGE = 86400                  # seconds clips may be cached (browser, Cloudflare)
MAX_FFMPEG = 2                        # clips cut at the same time; more requests wait


def client_ip():
    """The visitor's address. Behind Cloudflare + Traefik the connection comes from the
    proxy, so use the header Cloudflare sets (CF-Connecting-IP), then X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return request.headers.get("CF-Connecting-IP") or forwarded or request.remote_addr


# Limits are per visitor, kept in memory: run ONE gunicorn worker (threads are fine).
# Clips: a search page fetches up to 10 at once (Compartir pre-downloads them). The
# application-wide clip limit holds even if someone fakes the IP headers.
limiter = Limiter(client_ip, app=app, storage_uri="memory://")
ffmpeg_slots = threading.BoundedSemaphore(MAX_FFMPEG)

if not os.path.exists(SNIPPET_DIR):
    os.makedirs(SNIPPET_DIR)

obs.init(app, SNIPPET_DIR)      # metrics port, JSON logs, traces (see observability.py)

# Search index over every sung word of every song (a few thousand rows), loaded
# once and reloaded when the database file changes.
_index = {"mtime": None, "index": None}


def get_index():
    mtime = os.path.getmtime(DB_PATH)
    if _index["mtime"] != mtime:
        t0 = time.perf_counter()
        with obs.tracer.start_as_current_span("index.load"):
            # read-only: on the server the database sits on a read-only mount
            with closing(sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)) as conn:
                try:
                    rows = conn.execute("SELECT song, w, start, end, line, tok, orig FROM words ORDER BY song, i").fetchall()
                except sqlite3.OperationalError:     # index built before line numbers: no lines shown
                    rows = conn.execute("SELECT song, w, start, end, NULL, i, w FROM words ORDER BY song, i").fetchall()
            ix = Index(rows)
        _index.update(mtime=mtime, index=ix)
        obs.INDEX_SONGS.set(len(ix.songs))
        obs.INDEX_WORDS.set(len(rows))
        obs.INDEX_LOADED.set_to_current_time()
        obs.log("index_loaded", songs=len(ix.songs), words=len(rows), duration_ms=round((time.perf_counter() - t0) * 1000))
    return _index["index"]


def title(song):
    return song.replace('_', ' ').title()


def clock(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def file_name(song, tokens):
    """'La Bestia Pop - A brillar mi amor.mp3': song + the highlighted words, safe for any OS."""
    words = re.sub(r"[^\w\s'-]", "", " ".join(w for w, marked, _ in tokens if marked))
    words = " ".join(words.split())[:60].strip()
    return f"{title(song)} - {words}.mp3" if words else f"{title(song)}.mp3"


def clip_url(song, start, end):
    return url_for('clip', song=song, start_ms=int(start * 1000), end_ms=int(end * 1000))


# Load the search index now, while the pod starts (the startup probe waits for it), so
# /healthz and the first search don't pay for it: ~1 s with the pod's CPU limit.
try:
    get_index()
except Exception as e:      # /healthz will report it (500) and the probes keep the pod out
    obs.log("index_load_failed", level="error", error=repr(e))


# Clip reports need a writable place; without one the site still works, /report answers 503.
try:
    reports = ReportStore(REPORTS_DB)
except sqlite3.Error as e:
    reports = None
    obs.log("reports_unavailable", level="error", path=REPORTS_DB, error=repr(e))


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/healthz')
def healthz():
    """For the Kubernetes probes: the search database can be loaded. Not logged or traced."""
    get_index()
    return "ok"


@app.errorhandler(429)
def too_many_requests(e):
    return "Demasiadas búsquedas seguidas. Esperá un minuto y probá de nuevo.", 429


@app.route('/search', methods=['GET', 'POST'])
@limiter.limit("30/minute;300/hour")
def search():
    user_query = request.values.get('lyric', '').strip()
    ix = get_index()
    t0 = time.perf_counter()
    with obs.tracer.start_as_current_span("search") as span:
        hits = ix.search(user_query, MAX_RESULTS)
        span.set_attribute("search.results", len(hits))
    seconds = time.perf_counter() - t0
    results = []
    for hit in hits:
        if not os.path.exists(os.path.join(MUSIC_DIR, f"{hit.song}.mp3")):
            continue
        tokens = ix.line_tokens(hit)
        results.append({
            'song': title(hit.song),
            'kind': hit.kind,
            'tokens': tokens,
            'filename': file_name(hit.song, tokens),
            'time': clock(hit.start),
            'clip': clip_url(hit.song, hit.start, hit.end),
            'others': [{'time': clock(o.start), 'clip': clip_url(o.song, o.start, o.end)} for o in hit.others],
        })
    exact = any(r['kind'] != 'approx' for r in results)     # 'partial' = what was typed, completed
    # outcome = the best result's kind: exact > partial (completed) > approx (typos/closest line) > none
    kinds = {r['kind'] for r in results}
    outcome = next((k for k in ('exact', 'partial', 'approx') if k in kinds), 'none')
    obs.SEARCHES.labels(outcome).inc()
    obs.SEARCH_SECONDS.observe(seconds)
    obs.log("search", query=user_query[:200], results=len(results), outcome=outcome,
            top_song=hits[0].song if hits else None, duration_ms=round(seconds * 1000, 1))
    return render_template('index.html', query=user_query, results=results, exact=exact, searched=True)


@app.route('/suggest')
@limiter.limit("120/minute")
def suggest():
    return jsonify([{'text': text, 'song': title(song)} for text, song in get_index().suggest(request.args.get('q', ''))])


def prune_cache():
    """Delete the oldest clips while the cache is over CACHE_MB (to 80% of it)."""
    files = []
    for entry in os.scandir(SNIPPET_DIR):
        if entry.is_file():
            st = entry.stat()
            files.append((st.st_mtime, st.st_size, entry.path))
    total = sum(size for _, size, _ in files)
    if total <= CACHE_MB * 2**20:
        return
    for _, size, path in sorted(files):
        if total <= CACHE_MB * 2**20 * 0.8:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


_cuts = {"n": 0}


@app.route('/clip/<song>/<int:start_ms>-<int:end_ms>.mp3')
@limiter.limit("60/minute;600/hour")
@limiter.shared_limit("600/minute", scope="all-clips", key_func=lambda: "all")
def clip(song, start_ms, end_ms):
    """The sung phrase from start to end (ms) plus padding, cut on first request and cached.

    ?name=<file name> sends it as a download with that name (Descargar button).
    """
    kind = 'download' if request.args.get('name') else 'play'
    if song not in get_index().songs or not 0 <= start_ms < end_ms <= start_ms + MAX_CLIP_S * 1000:
        abort(404)
    start, end = start_ms / 1000, end_ms / 1000
    # File name includes the exact timing (ms), so a rebuilt index never serves stale clips
    output_path = os.path.join(SNIPPET_DIR, f"{song.replace(' ', '_')}_{start_ms}_{end_ms}.mp3")
    input_path = os.path.join(MUSIC_DIR, f"{song}.mp3")
    result, ffmpeg_ms = 'cached', None
    if not os.path.exists(output_path):
        if not os.path.exists(input_path):
            abort(404)
        # Fast clip with FFmpeg, into a temporary file: concurrent requests never see a half-written clip
        obs.FFMPEG_ACTIVE.inc()
        try:
            with ffmpeg_slots, obs.tracer.start_as_current_span("clip.ffmpeg") as span:
                span.set_attribute("clip.song", song)
                t0 = time.perf_counter()
                fd, tmp_path = tempfile.mkstemp(suffix='.mp3', dir=SNIPPET_DIR)
                os.close(fd)
                error = None
                try:
                    run = subprocess.run([
                        'ffmpeg', '-v', 'error', '-y', '-ss', str(max(0, start - PAD_BEFORE)), '-to', str(end + PAD_AFTER),
                        '-i', input_path, '-c', 'copy', tmp_path
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
                    if run.returncode != 0:
                        error = f"exit {run.returncode}: {run.stderr.decode(errors='replace')[-500:]}"
                except subprocess.TimeoutExpired:
                    error = "timeout after 30 s"
                if error is None:
                    os.replace(tmp_path, output_path)
                else:
                    os.remove(tmp_path)
                    span.set_status(obs.trace.Status(obs.trace.StatusCode.ERROR, error))
                seconds = time.perf_counter() - t0
        finally:
            obs.FFMPEG_ACTIVE.dec()
        obs.FFMPEG_SECONDS.observe(seconds)
        result, ffmpeg_ms = ('cut', round(seconds * 1000)) if error is None else ('failed', round(seconds * 1000))
        if error:
            obs.log("clip_failed", level="error", song=song, start_ms=start_ms, end_ms=end_ms, error=error)
        _cuts["n"] += 1
        if _cuts["n"] % 50 == 0:
            prune_cache()
    obs.CLIPS.labels(result, kind).inc()
    obs.log("clip", song=song, start_ms=start_ms, end_ms=end_ms, result=result, kind=kind, ffmpeg_ms=ffmpeg_ms)
    if not os.path.exists(output_path):
        abort(500)
    # The URL holds the exact timing, so a clip never changes: browsers and Cloudflare may keep it
    name = os.path.basename(request.args.get('name', ''))
    if name:
        return send_file(output_path, mimetype='audio/mpeg', as_attachment=True, download_name=name, max_age=CLIP_MAX_AGE)
    return send_file(output_path, mimetype='audio/mpeg', max_age=CLIP_MAX_AGE)


SHARE_RESULTS = {'shared', 'cancelled', 'failed'}


@app.route('/event', methods=['POST'])
@limiter.limit("30/minute")
def event():
    """What happened after Compartir (only the page knows): {"type": "share", "result": ...}."""
    data = request.get_json(silent=True, force=True) or {}
    if data.get('type') != 'share' or data.get('result') not in SHARE_RESULTS:
        abort(400)
    obs.SHARES.labels(data['result']).inc()
    obs.log("share", result=data['result'], song=str(data.get('song', ''))[:100] or None)
    return '', 204


def _ms(value, name):
    """A time in ms from the request: an int ≥ 0 (or abort 400)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        abort(400, f"{name}: integer milliseconds expected")
    return value


def _clip_range(start_ms, end_ms, name):
    if not start_ms < end_ms <= start_ms + MAX_CLIP_S * 1000:
        abort(400, f"{name}: start must be before end, at most {MAX_CLIP_S} s apart")


@app.route('/report', methods=['POST'])
@limiter.limit("10/minute;50/day")
def report():
    """A visitor says a clip doesn't match (the ¿No coincide? panel of a card).

    {"song", "start_ms", "end_ms": the clip as served; "problem": one of reports.PROBLEMS;
     "fixed_start_ms", "fixed_end_ms": optional adjustment; "query", "line": context}
    """
    if reports is None:
        abort(503)
    data = request.get_json(silent=True, force=True)
    if not isinstance(data, dict):
        abort(400, "JSON object expected")
    song = data.get('song')
    if song not in get_index().songs:
        abort(400, "unknown song")
    if data.get('problem') not in PROBLEMS:
        abort(400, "unknown problem")
    start_ms, end_ms = _ms(data.get('start_ms'), "start_ms"), _ms(data.get('end_ms'), "end_ms")
    _clip_range(start_ms, end_ms, "clip")
    fixed_start, fixed_end = data.get('fixed_start_ms'), data.get('fixed_end_ms')
    if (fixed_start is None) != (fixed_end is None):
        abort(400, "fixed_start_ms and fixed_end_ms go together")
    if fixed_start is not None:
        fixed_start, fixed_end = _ms(fixed_start, "fixed_start_ms"), _ms(fixed_end, "fixed_end_ms")
        _clip_range(fixed_start, fixed_end, "fixed")
    text = lambda key: str(data.get(key) or '')[:200] or None
    row = {'song': song, 'start_ms': start_ms, 'end_ms': end_ms, 'problem': data['problem'],
           'fixed_start_ms': fixed_start, 'fixed_end_ms': fixed_end, 'query': text('query'), 'line': text('line'),
           'db_version': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_index["mtime"])) if _index["mtime"] else None}
    report_id = reports.add(row)
    obs.CLIP_REPORTS.labels(row['problem']).inc()
    obs.log("clip_report", id=report_id, song=song, problem=row['problem'], start_ms=start_ms, end_ms=end_ms,
            shift_start_ms=None if fixed_start is None else fixed_start - start_ms,
            shift_end_ms=None if fixed_end is None else fixed_end - end_ms)
    return jsonify(id=report_id), 201


if __name__ == "__main__":
    # Local development only (the container runs gunicorn). FLASK_DEBUG=1 turns on the
    # debugger, which lets anyone who can reach the page run code: never on a public server.
    app.run(host='0.0.0.0', port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
