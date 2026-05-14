FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

RUN groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --home-dir /app --shell /sbin/nologin app \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py state.py strava_client.py hevy_client.py poller.py \
     matcher.py canonical.py provider.py strava_provider.py \
     hevy_provider.py backfill.py ./
COPY templates/ ./templates/

RUN mkdir -p /data && chown -R app:app /app /data
USER app

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
