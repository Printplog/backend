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

## Direct BNB Chain payment gateway

The wallet app can receive and distribute USDT directly on BNB Smart Chain.
Deploy the migration first, then configure the same values on the web, Celery
worker, and Celery beat services:

```text
PAYMENT_GATEWAY_PROVIDER=bsc
BSC_RPC_URL=https://your-dedicated-bsc-rpc.example
BSC_CHAIN_ID=56
BSC_USDT_CONTRACT_ADDRESS=0x55d398326f99059fF775485246999027B3197955
BSC_GATEWAY_WALLET_ADDRESS=0xYourDedicatedGatewayWallet
BSC_GATEWAY_PRIVATE_KEY=your-protected-runtime-secret
BSC_REQUIRED_CONFIRMATIONS=3
BSC_RPC_TIMEOUT_SECONDS=15
BSC_GAS_LIMIT_MULTIPLIER_PERCENT=120
BSC_LIVE_PAYOUTS_ENABLED=False
BSC_LIVE_SWEEPS_ENABLED=False
BSC_SWEEP_GAS_FUNDING_MULTIPLIER_PERCENT=125
```

`BSC_GATEWAY_PRIVATE_KEY` must control `BSC_GATEWAY_WALLET_ADDRESS`. Store it
only in the deployment platform's encrypted secret store; never commit it or
send it through logs or chat. Keep enough BNB in this wallet for transaction
gas. Use a dedicated, limited-balance gateway wallet rather than the main
treasury wallet.

When `BSC_LIVE_SWEEPS_ENABLED=True`, each confirmed unique deposit address is
funded with only the BNB it needs for gas, then its full USDT balance is swept
into `BSC_GATEWAY_WALLET_ADDRESS`. Signed transactions and hashes are persisted
before broadcast so worker retries do not create duplicate transfers.

Start with `BSC_LIVE_PAYOUTS_ENABLED=False`. Verify a small real deposit and
confirm that it credits once, then fund the wallet with a small amount of BNB
and enable live payouts for a controlled distribution test. The old CPay
variables may remain during the rollback window but are ignored while
`PAYMENT_GATEWAY_PROVIDER=bsc`.
