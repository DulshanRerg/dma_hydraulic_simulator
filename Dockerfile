# Dockerfile — EPyT-Flow edition
# Multi-stage build for the EPANET-backed backend Hydraulic Simulation Service.

# ── stage 1: build ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# EPyT-Flow requires gcc + fortran for some numpy/scipy deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ gfortran libgfortran5 make && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt

# ── stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="Dulshan AS Rajabu"
LABEL description="EPANET-backed backend Hydraulic Simulation Service (EPyT-Flow engine)"

# EPyT-Flow's EPANET C library needs libgomp at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser
WORKDIR /app

COPY --from=builder /venv /venv
COPY app/ ./app/
COPY frontend/ ./frontend/

RUN mkdir -p /data/gpkg /data/inp /data/reports /data/db /tmp && \
    chown -R appuser:appuser /app /data

USER appuser

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# Render injects $PORT; fall back to 8080 for local docker-compose use
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]