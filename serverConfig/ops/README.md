# Ops artifacts — Dokploy / Coolify deployment

This folder holds **deployment plans, reference configs, and Postgres-side
changes** for the SharpToolz backend. Nothing here is auto-loaded by Django.

The actual services are defined by **Dockerfiles at the backend root**:

| Service           | Dockerfile               | Status       |
|-------------------|--------------------------|--------------|
| sharptoolz-web    | `Dockerfile`             | existing (CMD change pending — see ROLLOUT) |
| sharptoolz-ws     | `Dockerfile.ws`          | **new**      |
| sharptoolz-celery | `Dockerfile.worker`      | existing     |
| sharptoolz-beat   | `Dockerfile.beat`        | existing     |
| sharptoolz-pgbouncer | `Dockerfile.pgbouncer`| **new**      |

## What's in this folder

| File | Purpose |
|------|---------|
| `ROLLOUT.md`              | **Read this first.** Step-by-step Dokploy deploy order with rollback points. |
| `pgbouncer.ini.reference` | Reference values that the `edoburu/pgbouncer` image generates from your env vars. Editing this file does nothing — it's docs. Change behavior via Dokploy env vars. |
| `postgresql.tuning.conf`  | Append-include for `postgresql.conf` if you manage your own Postgres. Skip if PG is a managed Dokploy service or external managed DB. |
| `pg_stat_statements.sql`  | One-shot SQL to enable the query-stats extension. |
| `MONITORING.md`           | Netdata install (Contabo VPS host-level) + Django slow-query logging notes. |

## Networking model under Dokploy / Coolify

All services live on the same Docker network. They reach each other by
**service name** (the slug you set in the Dokploy UI):

```
[browser] → Traefik → sharptoolz-web   (HTTP)
                  └─→ sharptoolz-ws    (WebSocket — /ws/* path rule)

sharptoolz-web ─────┐
sharptoolz-ws ──────┼──→ sharptoolz-pgbouncer:6432 ──→ <postgres>:5432
sharptoolz-celery ──┤
sharptoolz-beat ────┘

sharptoolz-* ────────→ <redis>:6379   (channel layer + cache + celery broker)
```

If Postgres / Redis are **also** Dokploy services in the same project, they're
reachable by their Dokploy service name. If they're external (Contabo's
managed DB or a sibling VPS), point `DB_HOST` and `REDIS_URL` at their public
hostnames.

## Env vars — split by service

Set these in Dokploy's "Environment" tab per service. Variables marked **SAME
EVERYWHERE** must match across all services that touch the DB or Redis.

### All app services (web, ws, worker, beat) — **SAME EVERYWHERE**
```
ENV=production
SECRET_KEY=<long random>
JWT_SIGNING_KEY=<long random>
DEBUG=False
ALLOWED_HOSTS=api.sharptoolz.com,sharptoolz.com,…
DATABASE_URL=postgres://USER:PASS@sharptoolz-pgbouncer:6432/sharptoolz
REDIS_URL=redis://<redis-host>:6379/1
CELERY_BROKER_URL=redis://<redis-host>:6379/1
CELERY_RESULT_BACKEND=redis://<redis-host>:6379/1
TRUSTED_PROXY_HOPS=1
SENTRY_DSN=…
```

The web and worker services must also use the same private render storage. Set
the following identically on at least `sharptoolz-web` and
`sharptoolz-celery` (and on other services that read media):

```text
AWS_STORAGE_BUCKET_NAME=…
AWS_ACCESS_KEY_ID=…
AWS_SECRET_ACCESS_KEY=…
AWS_S3_ENDPOINT_URL=…
AWS_S3_REGION_NAME=…
AWS_S3_CUSTOM_DOMAIN=…
```

If object storage is intentionally disabled, mount the same `/app/media`
volume into web and worker and ensure uid/gid `10001` can write it. A database
job marked `completed` is not sufficient: the web container must be able to
open the artifact written by the worker.

### Additional per-service tuning

| Service | Variable | Suggested | Why |
|---------|----------|-----------|-----|
| web     | `WEB_CONCURRENCY` | `3` | gunicorn worker count (see ROLLOUT for CMD swap) |
| ws      | (defaults work)   | — | Daphne is single-process, asyncio loop |
| worker  | `CELERY_CONCURRENCY` | `2` | bounds DB connection use; default in your Dockerfile.worker is 4 |
| beat    | (none extra)      | — | |

### sharptoolz-pgbouncer service
```
DB_HOST=<postgres host>          # internal Dokploy name OR external host
DB_PORT=5432
DB_USER=<your PG user>
DB_PASSWORD=<your PG password>
DB_NAME=sharptoolz
POOL_MODE=transaction
DEFAULT_POOL_SIZE=25
MAX_CLIENT_CONN=400
```

## Dokploy Traefik routing

In Dokploy, for the **sharptoolz-web** service set the domain rule to your
main host (e.g. `api.sharptoolz.com`). For **sharptoolz-ws**, add a path-prefix
rule **with higher priority** so `/ws/*` wins over the web service:

- Host: `api.sharptoolz.com`
- Path: `/ws`
- Priority: `100` (any number higher than the web service's, which defaults to 0)

Coolify uses the same Traefik labels; the UI is similar.

## Important: DATABASES tweaks for transaction-mode pooling

When you flip to PgBouncer transaction-mode, edit `serverConfig/settings.py`
DATABASES block:

```python
'CONN_MAX_AGE': 0,                # don't double-pool
'DISABLE_SERVER_SIDE_CURSORS': True,
```

`ROLLOUT.md` tells you the exact moment to make this change so it lines up
with the env var DATABASE_URL switch.
