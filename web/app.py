from flask import Flask, abort, g, jsonify, make_response, redirect, render_template, request, send_file, url_for
from flask_limiter import Limiter
from contextlib import closing
import sqlite3
import re
import subprocess
import tempfile
import threading
import time
import os
import functools

import observability as obs
import access
import fixes
from reports import OLD_PROBLEMS, PROBLEMS, ReportStore
from search import Index

app = Flask(__name__)

# Config
DB_PATH = os.getenv("DB_PATH", "redondos.db")
MUSIC_DIR = os.getenv("LIBRARY_PATH")
# Vocal stems (<song>.mp3, Demucs, same timeline as the songs) for the timing editor; a song
# without one falls back to the song itself. Default: vocals/ next to the songs' folder.
VOCALS_DIR = os.getenv("VOCALS_PATH") or os.path.join(os.path.dirname(os.path.abspath(MUSIC_DIR or ".")), "vocals")
SNIPPET_DIR = os.getenv("SNIPPET_CACHE_DIR") or os.path.join(app.static_folder, "snippets")
MAX_RESULTS = 10
MAX_CLIP_S = 60
MAX_VOICE_S = MAX_CLIP_S + 10         # the editor's window: the clip plus 5 s on each side
VOICE_RATE = 16000                    # Hz: plenty to see and hear a voice, 32 kB per second
CACHE_MB = int(os.getenv("SNIPPET_CACHE_MB", "300"))   # oldest clips deleted above this
REPORTS_DB = os.getenv("REPORTS_DB", "reports.db")     # clip reports (writable; see reports.py)
REPORTS_RECHECK_S = float(os.getenv("REPORTS_RECHECK_S", "10"))   # how often reviews reach the index
# Timing reports applied at once (provisional fixes, see reports.py). AUTO_APPLY_REPORTS=0 turns it
# off: new reports wait for a review and the provisional ones already stored stop being applied.
AUTO_APPLY = os.getenv("AUTO_APPLY_REPORTS", "1") != "0"
AUTO_APPLY_MAX_SHIFT_MS = 5000        # an edge moved more than this waits for a review (the editor shows ±5 s)
AUTO_APPLY_PER_DAY = int(os.getenv("AUTO_APPLY_PER_DAY", "5"))    # provisional fixes per visitor per day
CLIP_MAX_AGE = 86400                  # seconds clips may be cached (browser, Cloudflare)
MAX_FFMPEG = 2                        # clips cut at the same time; more requests wait
# The site's public address, for the link preview tags (og:url, og:image must be absolute). Behind
# Cloudflare → Traefik the request arrives over http, so it's set in k8s/; locally the request's own.
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")


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

# Search index over every sung word of every song (a few thousand rows), loaded once
# and rebuilt when the database file changes or when the provisional timing fixes from
# visitors' reports change (checked every REPORTS_RECHECK_S, and right after a report).
_index = {"mtime": None, "rows": None, "baked": set(), "fixes": None, "checked": 0.0, "index": None}
_index_lock = threading.Lock()


def load_base():
    """The search database: its rows and the reports already baked into it (fixes table)."""
    with obs.tracer.start_as_current_span("index.load"):
        # read-only: on the server the database sits on a read-only mount
        with closing(sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)) as conn:
            try:
                rows = conn.execute("SELECT song, w, start, end, line, tok, orig FROM words ORDER BY song, i").fetchall()
            except sqlite3.OperationalError:     # index built before line numbers: no lines shown
                rows = conn.execute("SELECT song, w, start, end, NULL, i, w FROM words ORDER BY song, i").fetchall()
            try:
                baked = {r for (r,) in conn.execute("SELECT report FROM fixes")}
            except sqlite3.OperationalError:     # built before the fixes table
                baked = set()
    return rows, baked


def with_fixes(rows, reports_rows):
    """rows with the reports' adjustments applied (web/fixes.py). Returns (rows, applied, problems)."""
    if not reports_rows:
        return rows, [], []
    words = {}
    for song, _, start, end, _, tok, orig in rows:
        words.setdefault(song, {})[tok] = (orig, start, end)
    times, _, applied, problems = fixes.apply(words, [fixes.from_report(r) for r in reports_rows])
    out = [(song, w, round(times[song][tok][0], 3), round(times[song][tok][1], 3), line, tok, orig)
           for song, w, _, _, line, tok, orig in rows]
    return out, applied, problems


def get_index():
    now = time.monotonic()
    mtime = os.path.getmtime(DB_PATH)
    if _index["mtime"] == mtime and now - _index["checked"] < REPORTS_RECHECK_S:
        return _index["index"]
    with _index_lock:
        t0 = time.perf_counter()
        base_changed = _index["mtime"] != mtime
        if base_changed:
            _index["rows"], _index["baked"] = load_base()
        reports_rows = [r for r in reports.applied() if fixes.report_key(r) not in _index["baked"]
                        and (AUTO_APPLY or r["status"] == "accepted")] if reports else []
        _index["checked"] = now
        if base_changed or reports_rows != _index["fixes"]:
            rows, applied, problems = with_fixes(_index["rows"], reports_rows)
            ix = Index(rows)
            _index.update(mtime=mtime, fixes=reports_rows, index=ix)
            obs.INDEX_SONGS.set(len(ix.songs))
            obs.INDEX_WORDS.set(len(rows))
            obs.INDEX_LOADED.set_to_current_time()
            obs.log("index_loaded", songs=len(ix.songs), words=len(rows), provisional_fixes=len(applied),
                    fix_problems=problems or None, duration_ms=round((time.perf_counter() - t0) * 1000))
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


# Clip reports need a writable place; without one the site still works, /report answers 503.
try:
    reports = ReportStore(REPORTS_DB)
except sqlite3.Error as e:
    reports = None
    obs.log("reports_unavailable", level="error", path=REPORTS_DB, error=repr(e))


# Load the search index now, while the pod starts (the startup probe waits for it), so
# /healthz and the first search don't pay for it: ~1 s with the pod's CPU limit.
try:
    get_index()
except Exception as e:      # /healthz will report it (500) and the probes keep the pod out
    obs.log("index_load_failed", level="error", error=repr(e))


@app.context_processor
def site_url():
    return {'site_url': SITE_URL or request.url_root.rstrip("/")}


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


def cut(ffmpeg_args, output_path, song, start_ms, end_ms):
    """Runs ffmpeg into a temporary file, then moves it to output_path: concurrent requests
    never see a half-written file. At most MAX_FFMPEG at once. Returns (result, ffmpeg_ms)."""
    obs.FFMPEG_ACTIVE.inc()
    try:
        with ffmpeg_slots, obs.tracer.start_as_current_span("clip.ffmpeg") as span:
            span.set_attribute("clip.song", song)
            t0 = time.perf_counter()
            fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(output_path)[1], dir=SNIPPET_DIR)
            os.close(fd)
            error = None
            try:
                run = subprocess.run(['ffmpeg', '-v', 'error', '-y', *ffmpeg_args, tmp_path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
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
    if error:
        obs.log("clip_failed", level="error", song=song, start_ms=start_ms, end_ms=end_ms, error=error)
    _cuts["n"] += 1
    if _cuts["n"] % 50 == 0:
        prune_cache()
    return ('cut' if error is None else 'failed'), round(seconds * 1000)


@app.route('/clip/<song>/<int:start_ms>-<int:end_ms>.mp3')
@limiter.limit("60/minute;600/hour")
@limiter.shared_limit("600/minute", scope="all-clips", key_func=lambda: "all")
def clip(song, start_ms, end_ms):
    """The sung phrase from start to end (ms), exactly (no padding: visitors' timing fixes make the
    edges right), cut on first request and cached.

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
        # Fast clip: copies the mp3 frames, no re-encoding
        result, ffmpeg_ms = cut(['-ss', str(start), '-to', str(end),
                                 '-i', input_path, '-c', 'copy'], output_path, song, start_ms, end_ms)
    obs.CLIPS.labels(result, kind).inc()
    obs.log("clip", song=song, start_ms=start_ms, end_ms=end_ms, result=result, kind=kind, ffmpeg_ms=ffmpeg_ms)
    if not os.path.exists(output_path):
        abort(500)
    # The URL holds the exact timing, so a clip never changes: browsers and Cloudflare may keep it
    name = os.path.basename(request.args.get('name', ''))
    if name:
        return send_file(output_path, mimetype='audio/mpeg', as_attachment=True, download_name=name, max_age=CLIP_MAX_AGE)
    return send_file(output_path, mimetype='audio/mpeg', max_age=CLIP_MAX_AGE)


@app.route('/voice/<song>/<int:start_ms>-<int:end_ms>.wav')
@limiter.limit("30/minute;300/hour")
@limiter.shared_limit("600/minute", scope="all-clips", key_func=lambda: "all")
def voice(song, start_ms, end_ms):
    """The singer alone from start to end (ms), no padding, for the timing editor of
    ¿No coincide?: mono 16-bit WAV (no codec delay: sample 0 is exactly start_ms), from
    the vocal stem, or from the song if there is none (header X-Voice-Source says which)."""
    if song not in get_index().songs or not 0 <= start_ms < end_ms <= start_ms + MAX_VOICE_S * 1000:
        abort(404)
    source, input_path = 'vocals', os.path.join(VOCALS_DIR, f"{song}.mp3")
    if not os.path.exists(input_path):
        source, input_path = 'song', os.path.join(MUSIC_DIR, f"{song}.mp3")
    output_path = os.path.join(SNIPPET_DIR, f"voice_{source}_{song.replace(' ', '_')}_{start_ms}_{end_ms}.wav")
    result, ffmpeg_ms = 'cached', None
    if not os.path.exists(output_path):
        if not os.path.exists(input_path):
            abort(404)
        # -ss before -i while re-encoding is exact; bitexact keeps the header a plain 44 bytes
        result, ffmpeg_ms = cut(['-ss', str(start_ms / 1000), '-t', str((end_ms - start_ms) / 1000), '-i', input_path,
                                 '-map', '0:a', '-map_metadata', '-1', '-ac', '1', '-ar', str(VOICE_RATE),
                                 '-c:a', 'pcm_s16le', '-fflags', '+bitexact', '-f', 'wav'],
                                output_path, song, start_ms, end_ms)
    obs.CLIPS.labels(result, 'voice').inc()
    obs.log("clip", song=song, start_ms=start_ms, end_ms=end_ms, result=result, kind='voice', source=source,
            ffmpeg_ms=ffmpeg_ms)
    if not os.path.exists(output_path):
        abort(500)
    response = send_file(output_path, mimetype='audio/wav', max_age=CLIP_MAX_AGE)
    response.headers['X-Voice-Source'] = source
    return response


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
    words = phrase_words(song, start_ms, end_ms)
    if words:
        row.update(first_tok=words[0].tok, last_tok=words[-1].tok, first_word=words[0].orig, last_word=words[-1].orig)
    # A timing correction is applied at once (provisional) until someone reviews it
    auto_apply = auto_apply_decision(row, words)
    provisional = auto_apply == 'applied'
    if provisional:
        row['status'] = 'provisional'
    report_id = reports.add(row)
    if provisional:
        _index["checked"] = 0.0     # the next search (the visitor's own, maybe) already has it
    obs.CLIP_REPORTS.labels(row['problem']).inc()
    obs.log("clip_report", id=report_id, song=song, problem=row['problem'], start_ms=start_ms, end_ms=end_ms,
            shift_start_ms=None if fixed_start is None else fixed_start - start_ms,
            shift_end_ms=None if fixed_end is None else fixed_end - end_ms, auto_apply=auto_apply)
    return jsonify(id=report_id, provisional=provisional), 201


# Provisional fixes made per visitor today: {(visitor, day): n}, in memory like the rate limits
_auto_applied = {}
_auto_applied_lock = threading.Lock()


def auto_apply_decision(row, words):
    """Whether a report becomes a provisional fix: 'applied', or why not (logged)."""
    if row['problem'] != 'timing' or row['fixed_start_ms'] is None:
        return 'not_timing'
    if (row['fixed_start_ms'], row['fixed_end_ms']) == (row['start_ms'], row['end_ms']):
        return 'unchanged'
    if not words:
        return 'words_not_found'
    if not AUTO_APPLY:
        return 'off'
    if max(abs(row['fixed_start_ms'] - row['start_ms']), abs(row['fixed_end_ms'] - row['end_ms'])) > AUTO_APPLY_MAX_SHIFT_MS:
        return 'too_far'
    day = time.strftime("%Y-%m-%d", time.gmtime())
    key = (client_ip(), day)
    with _auto_applied_lock:
        for k in [k for k in _auto_applied if k[1] != day]:
            del _auto_applied[k]
        if _auto_applied.get(key, 0) >= AUTO_APPLY_PER_DAY:
            return 'visitor_limit'
        _auto_applied[key] = _auto_applied.get(key, 0) + 1
    return 'applied'


def phrase_words(song, start_ms, end_ms):
    """The words a served clip was made of ([search.Word], first to last), or None.

    Clip URLs hold int(start * 1000) of the phrase's first word and int(end * 1000) of its
    last (clip_url), so the served times identify them while the index hasn't changed
    (±1 ms: float rounding). None for a page from before a database or fix change.
    """
    words = get_index().songs[song]
    for i, w in enumerate(words):
        if abs(int(w.start * 1000) - start_ms) <= 1:
            for j in range(i, len(words)):
                if abs(int(words[j].end * 1000) - end_ms) <= 1:
                    return words[i:j + 1]
    return None


# ------------------------------------------------------------------ admin (behind Cloudflare Access)

admin_access = access.Verifier()

PROBLEM_LABELS = {'timing': 'Está mal cortado', 'wrong_phrase': 'No es esta frase',
                  'unsure': 'Quisió... (No sabe, no responde)',
                  'starts_late': 'Empieza tarde', 'starts_early': 'Empieza antes',
                  'ends_early': 'Termina antes', 'ends_late': 'Termina tarde'}
assert set(PROBLEM_LABELS) == set(PROBLEMS) | set(OLD_PROBLEMS)
STATUS_LABELS = {None: 'Sin revisar', 'provisional': 'Aplicado provisoriamente',
                 'accepted': 'Aceptado', 'revoked': 'Rechazado'}
PENDING = (None, 'provisional')       # what still needs a review


def admin_only(view):
    """Only with a valid Cloudflare Access token (access.py); anyone else gets 404, as if the
    page didn't exist. The admin's email is in g.admin. Never cached, never indexed."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        g.admin = admin_access.admin(request.headers)
        if g.admin is None:
            abort(404)
        response = make_response(view(*args, **kwargs))
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response
    return wrapped


def clock_ms(ms):
    """93500 → '1:33.5'"""
    return f"{ms // 60000}:{ms % 60000 / 1000:04.1f}"


def admin_report(r):
    """A stored report as the admin list shows it."""
    item = dict(r, title=title(r['song']), problem_label=PROBLEM_LABELS.get(r['problem'], r['problem']),
                status_label=STATUS_LABELS.get(r['status'], r['status']), pending=r['status'] in PENDING,
                served=f"{clock_ms(r['start_ms'])} – {clock_ms(r['end_ms'])}",
                served_url=url_for('clip', song=r['song'], start_ms=r['start_ms'], end_ms=r['end_ms']))
    if r['fixed_start_ms'] is not None:
        shift = lambda ms: f"{ms / 1000:+.1f} s".replace('-', '−') if ms else None
        item.update(adjusted=f"{clock_ms(r['fixed_start_ms'])} – {clock_ms(r['fixed_end_ms'])}",
                    shift_start=shift(r['fixed_start_ms'] - r['start_ms']), shift_end=shift(r['fixed_end_ms'] - r['end_ms']),
                    adjusted_url=url_for('clip', song=r['song'], start_ms=r['fixed_start_ms'], end_ms=r['fixed_end_ms']))
    return item


@app.route('/admin')
@admin_only
def admin_home():
    return redirect(url_for('admin_reports'))


@app.route('/admin/reports')
@admin_only
def admin_reports():
    """Visitors' reports, newest first: the ones still to review (default) or all; ?song= one song."""
    if reports is None:
        abort(503)
    show = 'all' if request.args.get('show') == 'all' else 'pending'
    song = request.args.get('song') or None
    rows = [r for r in reversed(reports.all()) if song is None or r['song'] == song]
    counts = {'pending': sum(r['status'] in PENDING for r in rows), 'all': len(rows)}
    if show == 'pending':
        rows = [r for r in rows if r['status'] in PENDING]
    return render_template('admin_reports.html', reports=[admin_report(r) for r in rows], show=show,
                           song=song, song_title=title(song) if song else None, counts=counts, admin=g.admin)


if __name__ == "__main__":
    # Local development only (the container runs gunicorn). FLASK_DEBUG=1 turns on the
    # debugger, which lets anyone who can reach the page run code: never on a public server.
    app.run(host='0.0.0.0', port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
