FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py ./

# config.yaml is mounted at runtime — not baked in
# (see docker-compose.yml)

# Run as non-root for security
RUN useradd --no-create-home appuser
USER appuser

ENTRYPOINT ["python", "importer.py"]
