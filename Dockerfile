FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py ./
COPY templates/ templates/
COPY static/ static/

# Run as non-root (uid 1000 matches the first user on most Linux hosts, so the
# bind-mounted ./data directory is writable without extra steps).
RUN useradd --no-create-home --uid 1000 appuser \
    && mkdir /data && chown appuser /data
USER appuser

# config.yaml lives in the /data volume and is edited through the web UI.
ENV CONFIG_PATH=/data/config.yaml \
    ROOT_PATH=/private-charging \
    PORT=4001
EXPOSE 4001

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}{os.environ[\"ROOT_PATH\"].rstrip(\"/\")}/health')"

# Web UI + built-in scheduler.  One-off CLI run:
#   docker compose run --rm private-costs python importer.py --dry-run
CMD ["python", "web.py"]
