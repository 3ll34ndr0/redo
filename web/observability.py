"""Metrics, logs and traces for the app.

- Metrics: Prometheus, served on a separate port (METRICS_PORT, default 9100) that the
  Service/Ingress never expose; the in-cluster agent scrapes it. 0 turns it off.
- Logs: one JSON line per request and per event (search, clip, share) on stdout, with
  the trace id so a log line links to its trace. No visitor IPs.
- Traces: OpenTelemetry, a span per request plus spans for the slow parts (search,
  index load, ffmpeg). Exported only when OTEL_EXPORTER_OTLP_ENDPOINT is set (standard
  OpenTelemetry variable, e.g. http://alloy.monitoring:4318); otherwise off and free.
"""

import json
import os
import sys
import time
import traceback

from flask import g, got_request_exception, request
from opentelemetry import trace
from prometheus_client import Counter, Gauge, Histogram, start_http_server

SERVICE = os.getenv("OTEL_SERVICE_NAME", "extractos")
tracer = trace.get_tracer(SERVICE)

# --- metrics

REQUESTS = Counter("extractos_http_requests_total", "HTTP requests", ["route", "method", "status"])
REQUEST_SECONDS = Histogram("extractos_http_request_duration_seconds", "HTTP request duration", ["route"],
                            buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10))
SEARCHES = Counter("extractos_searches_total", "Searches by best result: exact, partial, approx, none", ["outcome"])
SEARCH_SECONDS = Histogram("extractos_search_duration_seconds", "Time to find and rank results",
                           buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1))
CLIPS = Counter("extractos_clips_total", "Clip requests: cached, cut (ffmpeg ran) or failed; and play or download",
                ["result", "kind"])
FFMPEG_SECONDS = Histogram("extractos_ffmpeg_duration_seconds", "Time ffmpeg takes to cut a clip",
                           buckets=(.025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))
FFMPEG_ACTIVE = Gauge("extractos_ffmpeg_in_progress", "Clips being cut or waiting for an ffmpeg slot")
RATE_LIMITED = Counter("extractos_rate_limited_total", "Requests refused by the rate limits", ["route"])
SHARES = Counter("extractos_shares_total", "Compartir button outcomes reported by the page: shared, cancelled, failed",
                 ["result"])
CACHE_BYTES = Gauge("extractos_clip_cache_bytes", "Size of the clip cache")
INDEX_SONGS = Gauge("extractos_index_songs", "Songs in the search index")
INDEX_WORDS = Gauge("extractos_index_words", "Sung words in the search index")
INDEX_LOADED = Gauge("extractos_index_loaded_timestamp_seconds", "When the search database was last (re)loaded")


# Every known label combination starts at 0, so the first search/clip/share after a start is
# counted too (Prometheus' increase() can't see the jump of a series that appears at 1).
for _outcome in ("exact", "partial", "approx", "none"):
    SEARCHES.labels(_outcome)
for _result in ("cached", "cut", "failed"):
    for _kind in ("play", "download"):
        CLIPS.labels(_result, _kind)
for _result in ("shared", "cancelled", "failed"):
    SHARES.labels(_result)


def dir_bytes(path):
    try:
        return sum(e.stat().st_size for e in os.scandir(path) if e.is_file())
    except OSError:
        return 0


# --- logs

def log(event, **fields):
    """One JSON line on stdout, with the current trace id when there is one."""
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
              "level": fields.pop("level", "info"), "event": event}
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        record["trace_id"] = format(ctx.trace_id, "032x")
        record["span_id"] = format(ctx.span_id, "016x")
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stdout, flush=True)


# --- wiring

def setup_tracing(app):
    """Trace every request (OpenTelemetry), exporting over OTLP/HTTP if an endpoint is set."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # health checks and metrics would only add noise
    FlaskInstrumentor().instrument_app(app, excluded_urls="healthz")
    return True


def init(app, cache_dir):
    """Metrics port, request logs/metrics and tracing for a Flask app."""
    CACHE_BYTES.set_function(lambda: dir_bytes(cache_dir))
    port = int(os.getenv("METRICS_PORT", "9100"))
    if port:
        start_http_server(port)
    tracing = setup_tracing(app)

    @app.before_request
    def start_timer():
        g.t0 = time.perf_counter()

    @app.after_request
    def record(response):
        if request.endpoint in (None, "static", "healthz"):
            return response
        seconds = time.perf_counter() - g.get("t0", time.perf_counter())
        route = request.url_rule.rule if request.url_rule else "unknown"
        REQUESTS.labels(route, request.method, response.status_code).inc()
        REQUEST_SECONDS.labels(route).observe(seconds)
        if response.status_code == 429:
            RATE_LIMITED.labels(route).inc()
        log("request", method=request.method, route=route, path=request.path, status=response.status_code,
            duration_ms=round(seconds * 1000, 1), level="warning" if response.status_code >= 400 else "info")
        return response

    def record_error(sender, exception, **extra):
        # the traceback of an unhandled exception, as one JSON line (the 500 itself is
        # counted and logged by record() above, which Flask still runs on the error page)
        route = request.url_rule.rule if request.url_rule else "unknown"
        log("error", level="error", method=request.method, route=route, path=request.path, status=500,
            error=repr(exception), traceback="".join(traceback.format_exception(exception)))

    got_request_exception.connect(record_error, app, weak=False)
    app.log_exception = lambda exc_info: None      # Flask's own multi-line traceback: logged above instead
    log("startup", metrics_port=port or None, tracing=tracing)
