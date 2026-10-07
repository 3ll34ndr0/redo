# Extractos: the web app only. Songs and the search database are NOT in the image
# (size + copyright): they're mounted read-only from the server (see kubernetes/).
FROM python:3.12-slim

# Standalone ffmpeg build (one file). Debian's ffmpeg package made the image 844 MB.
COPY --from=mwader/static-ffmpeg:7.1.1 /ffmpeg /usr/local/bin/

WORKDIR /app
COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web/app.py web/search.py web/textnorm.py web/observability.py ./
COPY web/templates/ templates/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/redondos.db \
    LIBRARY_PATH=/data/music \
    SNIPPET_CACHE_DIR=/cache

RUN useradd --uid 10001 --no-create-home app
USER 10001
# 5000: the site; 9100: Prometheus metrics (cluster-internal, never on the Ingress)
EXPOSE 5000 9100
# ONE worker: rate limits, metrics and the search index live in that process's memory.
# No gunicorn access log: the app writes one JSON line per request (observability.py).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "60", "app:app"]
