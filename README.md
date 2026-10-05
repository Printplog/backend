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

Rendering is asynchronous. A completed job contains a signed `download_url`
that lasts five minutes; retrieve `GET /api/v1/renders/{id}` to mint a fresh
URL. Artifacts are retained for `API_RENDER_RETENTION_HOURS` (24 hours by
default). Stable render errors are `queue_unavailable`, `render_timeout`,
`render_invalid_input`, `render_invalid_output`, `render_source_missing`,
`render_source_unreadable`, `renderer_unavailable`, `render_storage_failed`,
and `render_failed`.

## Resend tracking support email

ParcelFinda and MyFlightLookup support tickets use unique plus-addresses on
their existing domains; no support subdomain or individual mailbox is needed.
For example, a ticket receives replies at
`support+<ticket-uuid>@parcelfinda.com`. The same pattern is used on
`myflightlookup.com`.

Set both credentials in **Admin → Site Settings → Integrations**. Secret fields
are write-only: the API accepts replacements from a superuser after a fresh
authenticator code, encrypts them at rest, and returns only whether each value
is configured. Leaving a field blank preserves its current value.

The following environment variables remain supported as bootstrap fallbacks:

```text
RESEND_API_KEY=re_...
RESEND_WEBHOOK_SECRET=whsec_...
```

Set a stable `INTEGRATION_SECRET_ENCRYPTION_KEY` on every backend instance.
Changing this master key makes existing admin-managed integration secrets
unreadable. Future provider credentials should be added through the same
write-only integration-secret registry rather than as readable settings fields.

The API key must be allowed to send mail and retrieve received email content;
a sending-only restricted key cannot process inbound reply bodies.

These sender/domain settings already have production defaults and only need to
be set when the verified Resend addresses differ:

```text
PARCEL_SUPPORT_FROM_EMAIL=ParcelFinda Support <support@parcelfinda.com>
PARCEL_SUPPORT_DOMAIN=parcelfinda.com
FLIGHT_SUPPORT_FROM_EMAIL=MyFlightLookup Support <support@myflightlookup.com>
FLIGHT_SUPPORT_DOMAIN=myflightlookup.com
```

In Resend, create one webhook with endpoint
`https://api.sharptoolz.com/api/webhooks/resend/`. Subscribe it to
`email.received`, `email.sent`, `email.delivered`, `email.delivery_delayed`,
`email.bounced`, `email.failed`, `email.suppressed`, and `email.complained`.
Copy that webhook's signing secret into the admin Integrations page (or the
`RESEND_WEBHOOK_SECRET` bootstrap fallback).

The flow is bidirectional. A new public request is emailed to the SharpToolz
user who owns the matching tracked document. Replies from either the owner or
the customer are validated against the ticket participants, forwarded to the
other person, and stored in the dashboard conversation. Webhook requests are
verified against the raw request body and duplicate received emails are
ignored.

## Pusher realtime tracking support

The support dashboard, ParcelFinda, and MyFlightLookup can update active
conversations without polling. PostgreSQL remains the source of truth: Pusher
events contain only the ticket ID and update time, and authorized clients
refetch the conversation from the API. Resend continues to deliver email and
acts as the fallback when realtime is unavailable.

Create a Channels app in Pusher, then enter its **App ID**, **Key**, **Secret**,
and **Cluster** in **Admin → Site Settings → Integrations**. These values are
stored in the same encrypted, write-only registry as the Resend credentials.
The browser receives only the public Pusher key and cluster; the app ID and
secret never leave the backend.

The following environment variables remain available as bootstrap fallbacks:

```text
PUSHER_APP_ID=...
PUSHER_KEY=...
PUSHER_SECRET=...
PUSHER_CLUSTER=eu
```

Customer channels are private. On first contact the API returns a random
ticket access token once; the tracking site stores it locally and sends it in
the `X-Support-Token` header when reading the thread, replying, or authorizing
its Pusher channel. Only the authenticated document owner can authorize the
separate dashboard channel. A token grants access to one ticket only and is
stored server-side as an HMAC hash.

The floating widgets require only a valid tracking ID. They open an anonymous
realtime conversation and collect the actual question in the chat composer.
When no customer email is attached, owner replies are stored and delivered
through the private channel instead of being sent through Resend. The full
contact forms may still collect an email and retain bidirectional email
fallback for customers who use them.

Pusher is optional. Until all four values are configured, the APIs advertise
realtime as disabled and the UI keeps the existing email conversation flow.

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
