#!/usr/bin/env python3
"""Writes extractos-dashboard.json: the Grafana dashboard for Extractos.

Import it in Grafana (Dashboards → New → Import → upload the JSON) and pick the
Prometheus and Loki data sources when asked (in Grafana Cloud: grafanacloud-*-prom
and grafanacloud-*-logs). Metrics come from web/observability.py, logs are its JSON
lines collected from namespace extractos.

Usage: python3 make_dashboard.py   (from this folder)
"""

import json
import os

PROM = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
LOKI = {"type": "loki", "uid": "${DS_LOKI}"}
LOGS = '{namespace="extractos"}'                  # stream selector for the app's logs
SEARCHES = LOGS + ' | json | event="search"'

panels = []
_next = {"id": 1, "y": 0}


def add(panel, w, h, x):
    panel["id"] = _next["id"]
    _next["id"] += 1
    panel["gridPos"] = {"h": h, "w": w, "x": x, "y": _next["y"]}
    panels.append(panel)


def row(title):
    add({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1, 0)
    _next["y"] += 1


def end_row(h):
    _next["y"] += h


def prom(expr, legend="", instant=False):
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant:
        t.update(instant=True, range=False)
    return t


def loki(expr, instant=False, legend=""):
    t = {"datasource": LOKI, "expr": expr, "refId": "A", "queryType": "instant" if instant else "range",
         "legendFormat": legend}
    return t


def stat(title, expr, unit="short", description="", thresholds=None, decimals=0):
    return {
        "type": "stat", "title": title, "description": description, "datasource": PROM,
        "targets": [prom(expr, instant=True)],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "value"},
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}},
                        "overrides": []},
    }


def timeseries(title, targets, unit="short", description="", stacked=False, bars=False, overrides=None):
    for n, t in enumerate(targets):
        t["refId"] = chr(65 + n)
    custom = {"drawStyle": "bars" if bars else "line", "fillOpacity": 60 if bars else 10, "lineWidth": 1,
              "showPoints": "never", "spanNulls": True,
              "stacking": {"mode": "normal" if stacked else "none", "group": "A"}}
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": PROM,
        "targets": targets,
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": overrides or []},
    }


def color(name, c):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}


def logs_panel(title, expr, description=""):
    return {"type": "logs", "title": title, "description": description, "datasource": LOKI,
            "targets": [loki(expr)],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False}}


def top_table(title, expr, label, column, description=""):
    """Instant LogQL metric query (sum by <label>) shown as a two-column table."""
    return {"type": "table", "title": title, "description": description, "datasource": LOKI,
            "targets": [loki(expr, instant=True, legend="{{" + label + "}}")],
            "transformations": [
                {"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows"}},
                {"id": "organize", "options": {"renameByName": {"Field": column, "Last *": "Times", "Last": "Times"}}},
                {"id": "sortBy", "options": {"sort": [{"field": "Times", "desc": True}]}},
            ],
            "options": {"showHeader": True, "cellHeight": "sm"},
            "fieldConfig": {"defaults": {}, "overrides": []}}


# ------------------------------------------------------------------ overview (whole time range)

row("Overview (selected time range)")
green = {"mode": "absolute", "steps": [{"color": "green", "value": None}]}
add(stat("Searches", "sum(increase(extractos_searches_total[$__range]))", thresholds=green), 4, 4, 0)
add(stat('"Nothing found"', 'sum(increase(extractos_searches_total{outcome="none"}[$__range])) / sum(increase(extractos_searches_total[$__range]))',
         unit="percentunit", decimals=1, description="Share of searches with no result at all (not even a close line)",
         thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 0.2}, {"color": "red", "value": 0.4}]}),
    4, 4, 4)
add(stat("Clips played", 'sum(increase(extractos_clips_total{kind="play"}[$__range]))', thresholds=green,
         description="Includes the clips the page pre-downloads for Compartir. Repeat plays served by Cloudflare's cache never reach the app"),
    4, 4, 8)
add(stat("Downloads", 'sum(increase(extractos_clips_total{kind="download"}[$__range]))', thresholds=green), 4, 4, 12)
add(stat("Shares", 'sum(increase(extractos_shares_total{result="shared"}[$__range]))', thresholds=green,
         description="Compartir completed (reported by the page)"), 4, 4, 16)
add(stat("Server errors", 'sum(increase(extractos_http_requests_total{status=~"5.."}[$__range])) or vector(0)',
         thresholds={"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]},
         description="HTTP 5xx answers"), 4, 4, 20)
end_row(4)

# ------------------------------------------------------------------ usage over time

row("Usage")
per_min = "(searches per minute)"
add(timeseries("Searches by outcome", [prom('sum by (outcome) (rate(extractos_searches_total[$__rate_interval])) * 60', "{{outcome}}")],
               unit="short", stacked=True, bars=True,
               description="exact = typed phrase found; partial = last word completed; approx = typos or closest lines; none = nothing. Per minute",
               overrides=[color("exact", "green"), color("partial", "blue"), color("approx", "orange"), color("none", "red")]),
    12, 8, 0)
add(timeseries("Clips", [prom('sum by (kind, result) (rate(extractos_clips_total[$__rate_interval])) * 60', "{{kind}} {{result}}")],
               stacked=True, bars=True,
               description="cut = ffmpeg ran; cached = already on disk; failed = ffmpeg error. Per minute",
               overrides=[color("play cut", "blue"), color("play cached", "green"), color("download cut", "purple"),
                          color("download cached", "light-purple"), color("play failed", "red"), color("download failed", "red")]),
    12, 8, 12)
end_row(8)
add(timeseries("Shares (Compartir)", [prom('sum by (result) (rate(extractos_shares_total[$__rate_interval])) * 60', "{{result}}")],
               stacked=True, bars=True, description="As reported by the page after the share sheet closes. Per minute",
               overrides=[color("shared", "green"), color("cancelled", "yellow"), color("failed", "red")]),
    12, 8, 0)
add(timeseries("Requests by status", [prom('sum by (status) (rate(extractos_http_requests_total[$__rate_interval])) * 60', "{{status}}")],
               stacked=True, bars=True, description="429 = refused by the rate limits. Per minute",
               overrides=[color("200", "green"), color("204", "green"), color("404", "yellow"), color("429", "orange"), color("500", "red")]),
    12, 8, 12)
end_row(8)

# ------------------------------------------------------------------ performance

row("Performance")
add(timeseries("Response time p95 by page",
               [prom('histogram_quantile(0.95, sum by (le, route) (rate(extractos_http_request_duration_seconds_bucket[$__rate_interval])))', "{{route}}")],
               unit="s", description="95% of requests are faster than this"),
    8, 8, 0)
add(timeseries("Search time", [
    prom('histogram_quantile(0.5, sum by (le) (rate(extractos_search_duration_seconds_bucket[$__rate_interval])))', "p50"),
    prom('histogram_quantile(0.95, sum by (le) (rate(extractos_search_duration_seconds_bucket[$__rate_interval])))', "p95")],
               unit="s", description="Finding and ranking the results only (no page rendering)"),
    8, 8, 8)
add(timeseries("Clip cutting (ffmpeg)", [
    prom('histogram_quantile(0.95, sum by (le) (rate(extractos_ffmpeg_duration_seconds_bucket[$__rate_interval])))', "p95 time"),
    prom('max(extractos_ffmpeg_in_progress)', "cutting or waiting")],
               unit="s", description="Right axis: clips being cut or waiting for one of the 2 ffmpeg slots",
               overrides=[{"matcher": {"id": "byName", "options": "cutting or waiting"},
                           "properties": [{"id": "unit", "value": "short"}, {"id": "custom.axisPlacement", "value": "right"}]}]),
    8, 8, 16)
end_row(8)
add(timeseries("Rate-limited requests", [prom('sum by (route) (increase(extractos_rate_limited_total[$__rate_interval]))', "{{route}}")],
               bars=True, description="Requests refused with 429 (too many from one visitor, or 600 clips/min for everyone)"),
    8, 6, 0)
add(timeseries("Clip cache size", [prom('max(extractos_clip_cache_bytes)', "cache")], unit="bytes",
               description="The app deletes the oldest clips above 300 MB"),
    8, 6, 8)
add(timeseries("Search index", [prom('max(extractos_index_songs)', "songs"), prom('max(extractos_index_words)', "words")],
               description="Changes when a new redondos.db is copied to the server",
               overrides=[{"matcher": {"id": "byName", "options": "words"},
                           "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]),
    8, 6, 16)
end_row(6)

# ------------------------------------------------------------------ what people search (logs)

row("What people search (logs)")
add(top_table('Top "nothing found" searches',
              f'topk(20, sum by (query) (count_over_time({SEARCHES} | outcome="none" [$__range])))', "query", "Search",
              description="Searches with no result at all: missing songs, misaligned lyrics, or typos the search can't fix"),
    8, 10, 0)
add(top_table("Top searched phrases", f'topk(20, sum by (query) (count_over_time({SEARCHES} [$__range])))', "query", "Search"), 8, 10, 8)
add(top_table("Top songs (first result)",
              f'topk(20, sum by (top_song) (count_over_time({SEARCHES} | top_song!="" [$__range])))', "top_song", "Song",
              description="Song of the first card of each search"),
    8, 10, 16)
end_row(10)
add(logs_panel('Recent "nothing found" searches',
               f'{SEARCHES} | outcome="none" | line_format "{{{{.query}}}}"'), 12, 9, 0)
add(logs_panel("Errors", LOGS + ' |= "\\"level\\": \\"error\\""',
               description="Crashes (with traceback) and failed clip cuts. Open a line to see its trace_id"), 12, 9, 12)
end_row(9)

# ------------------------------------------------------------------ clip reports (¿No coincide?)

row('Clip reports ("¿No coincide?")')
add(timeseries("Reports by problem", [prom('sum by (problem) (increase(extractos_clip_reports_total[$__rate_interval]))', "{{problem}}")],
               bars=True, stacked=True,
               description="Visitors saying a clip doesn't match. Details: tools/reports.py export + summary",
               overrides=[color("starts_late", "orange"), color("starts_early", "yellow"), color("ends_early", "purple"),
                          color("wrong_phrase", "red")]),
    8, 9, 0)
add(top_table("Most reported songs",
              f'topk(20, sum by (song) (count_over_time({LOGS} | json | event="clip_report" [$__range])))', "song", "Song"),
    6, 9, 8)
add(logs_panel("Latest reports",
               LOGS + ' | json | event="clip_report" | line_format "{{.song}} · {{.problem}} · clip {{.start_ms}}-{{.end_ms}} ms · '
                      'correction start {{.shift_start_ms}} ms, end {{.shift_end_ms}} ms"',
               description="Corrections are what the visitor adjusted (empty: no adjustment)"),
    10, 9, 14)
end_row(9)

dashboard = {
    "__inputs": [
        {"name": "DS_PROMETHEUS", "label": "Prometheus", "description": "Metrics (grafanacloud-*-prom)",
         "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"},
        {"name": "DS_LOKI", "label": "Loki", "description": "Logs (grafanacloud-*-logs)",
         "type": "datasource", "pluginId": "loki", "pluginName": "Loki"},
    ],
    "__requires": [
        {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
        {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"},
        {"type": "datasource", "id": "loki", "name": "Loki", "version": "1.0.0"},
    ],
    "title": "Extractos",
    "uid": "extractos",
    "description": "https://extractos.marso.ar: searches, clips, shares, performance and errors",
    "tags": ["extractos", "marso"],
    "timezone": "browser",
    "editable": True,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "schemaVersion": 39,
    "panels": panels,
    "templating": {"list": []},
    "annotations": {"list": []},
    "links": [{"title": "Extractos", "type": "link", "url": "https://extractos.marso.ar", "targetBlank": True}],
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extractos-dashboard.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(dashboard, f, indent=2, ensure_ascii=False)
print(f"{out}: {len([p for p in panels if p['type'] != 'row'])} panels")
