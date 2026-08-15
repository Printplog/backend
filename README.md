# SharpToolz backend

## API platform deployment

Apply migrations before enabling access:

```bash
poetry run python manage.py migrate
```

Build the web service with `Dockerfile` and the Celery worker with
`Dockerfile.worker`. The worker image installs Chromium and runs as uid/gid
`10001`; it is required for `/api/v1/documents/{id}/render`.

Production requires PostgreSQL, Redis/Celery, `ENV=production`, exact
`ALLOWED_HOSTS` and `FRONTEND_URL` values, and independently generated strong
`SECRET_KEY` and `JWT_SIGNING_KEY` values. Use Full (strict) TLS between the
edge proxy and origin. Do not use Cloudflare Flexible TLS.

Set a separate high-entropy `API_KEY_PEPPER` before issuing the first live API
key. Keep it stable and identical across web instances; changing it revokes all
existing API keys and hosted-session tokens.

The web and worker services must share private media storage. Configure the
same `AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_S3_ENDPOINT_URL`, and optional
`AWS_S3_REGION_NAME`/`AWS_S3_CUSTOM_DOMAIN` values in both services. If object
storage is not configured, mount the same `/app/media` volume into both
containers and grant uid/gid `10001` write access.

Useful optional limits are:

- `API_RENDER_MAX_ACTIVE_PER_KEY` (default `10`)
- `API_RENDER_MAX_ACTIVE_PER_USER` (default `20`)
- `API_RENDER_MAX_OUTPUT_BYTES` (default `52428800`)
- `API_RENDER_STORAGE_BYTES_PER_USER` (default `1073741824`)
- `API_RENDER_RETENTION_HOURS` (default `24`)
- `API_EMBED_MAX_PENDING_PER_KEY` (default `500`)

The administrator enables the API, chooses whether activation is paid, and
sets the wallet price/rate limits in site settings. Customers then activate
from Settings -> API. API keys are shown once and belong only in customer
backend services.

The OpenAPI contract is served at `/api/v1/schema` and interactive docs at
`/api/v1/docs`. The hosted UI loader is served by the frontend at
`https://sharptoolz.com/embed/v1.js`.
