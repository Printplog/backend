# ------------------------------------------------------------------------------
#  Sharptoolz WebSocket Server (Daphne)
#  Deploy as a SEPARATE Dokploy/Coolify service from the HTTP service.
#  Route Traefik /ws/* to this service; everything else to sharptoolz-web.
# ------------------------------------------------------------------------------

# -- Stage 1: Build deps (mirrors Dockerfile so cache hits) -------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    pkg-config \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install poetry>=2.0.0

COPY pyproject.toml poetry.lock* ./
RUN poetry lock && poetry install --no-root && rm -rf $POETRY_CACHE_DIR


# -- Stage 2: Runtime ---------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# WS service doesn't need image/cairo libs — Daphne just brokers frames
# between the client and channels_redis. Smaller image = faster deploys.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY . .

# Healthcheck: Daphne should accept TCP on 8000.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:8000/ws/ \
        --header "Connection: Upgrade" --header "Upgrade: websocket" \
        -w '%{http_code}' | grep -qE '^(101|400|401|403|404|426)$' \
    || exit 1

EXPOSE 8000

# Daphne single-process; the asyncio loop multiplexes thousands of WS clients.
# DB pressure scales with asgiref's executor thread pool, not with WS count.
# Cap that pool via env so it never bursts past PgBouncer's room.
ENV ASGI_THREADS=10

CMD ["daphne", \
     "--bind", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--access-log", "-", \
     "serverConfig.asgi:application"]
