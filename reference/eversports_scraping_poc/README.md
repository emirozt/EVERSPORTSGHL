# Eversports → GoHighLevel Sync

Multi-tenant daemon that scrapes class booking data from Eversports and syncs participant contacts into GoHighLevel CRM.

## Architecture

- **One PM2 process per customer studio** — fully isolated per tenant
- **No login automation** — sessions are provided via Cookie-Editor export (30-day TTL)
- **Dedup fingerprint**: `email || classId || classDate` — never re-pushes a booking
- **Filesystem only** — no database; JSON state + CSV backup per tenant

## Requirements

- Node.js 20+
- PM2 (`npm install -g pm2`)
- Playwright Chromium (`npx playwright install chromium`)

## Quick Start (production VPS)

```bash
# On Ubuntu 22.04 — run as root or with sudo
sudo bash setup.sh

# Optional: set DOMAIN for automatic nginx + TLS
sudo DOMAIN=sync.youragency.com bash setup.sh
```

## Manual Setup

```bash
npm install
npx playwright install chromium
cp .env.example .env
```

## Provisioning a Tenant

```bash
node src/admin-cli.js add-tenant \
  --location-id <GHL_LOCATION_ID> \
  --ghl-key <GHL_API_KEY> \
  --label "Studio Name" \
  --notify-webhook https://hooks.yourcrm.com/session-expiry
```

The command prints a **secret key once** — store it securely. It is never recoverable (only the SHA-256 hash is stored).

## Admin CLI

```bash
# List all tenants with session expiry and daemon status
node src/admin-cli.js list-tenants

# Start / stop a tenant daemon
node src/admin-cli.js start-tenant <locationId>
node src/admin-cli.js stop-tenant <locationId>

# Remove a tenant (irreversible)
node src/admin-cli.js remove-tenant <locationId> --confirm

# Rotate upload secret
node src/admin-cli.js rotate-secret <locationId>
```

## Per-Tenant CLI

```bash
# One-off sync — today
node src/cli.js sync --env-file tenants/<locationId>/.env

# Sync last 7 days
node src/cli.js sync --env-file tenants/<locationId>/.env --days 7

# Dry run — scrape but don't write to GHL
node src/cli.js sync --env-file tenants/<locationId>/.env --dry-run

# Check session and sync status
node src/cli.js status --env-file tenants/<locationId>/.env

# Verify session is live against Eversports
node src/cli.js verify-session --env-file tenants/<locationId>/.env

# Reset dedup state
node src/cli.js clear-state --env-file tenants/<locationId>/.env
```

## Session Management

Sessions expire after ~30 days. Customers refresh them via the upload portal:

1. Visit `http://<server>:3000`
2. Enter Location ID + Secret key
3. Export cookies from Chrome using [Cookie-Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
4. Upload the JSON file

The daemon automatically:
- Warns `WARN_DAYS_BEFORE_EXPIRY` (default: 5) days before expiry via webhook POST
- Skips runs on expired sessions without crashing
- Resumes automatically after a fresh session is uploaded

### Webhook payload

```json
{
  "event": "session_expiring_soon" | "session_expired",
  "locationId": "LOC123",
  "daysLeft": 4,
  "expiresAt": "2024-08-15T10:00:00.000Z"
}
```

## Portal API

### `POST /upload`

Uploads a Cookie-Editor JSON export for a tenant.

**Form fields:**
| Field | Type | Description |
|---|---|---|
| `locationId` | string | GHL Location ID |
| `secret` | string | Tenant secret |
| `cookies` | file | Cookie-Editor JSON export |

**Response:** `{ success: true, expiresAt: "<ISO>" }`

### `GET /status/:locationId`

**Header:** `Authorization: Bearer <secret>`

**Response:**
```json
{
  "tenant": { "locationId": "...", "label": "..." },
  "active": true,
  "daemon": "online",
  "session": { "status": "valid", "daysLeft": 22, "expiresAt": "..." }
}
```

## File Structure

```
├── src/
│   ├── logger.js           Winston factory (per-tenant log path)
│   ├── state.js            Dedup fingerprint persistence
│   ├── session.js          Cookie import, expiry, session injection
│   ├── scraper.js          Playwright scraper (no login logic)
│   ├── ghl.js              GHL V2 API client
│   ├── csv.js              CSV backup writer
│   ├── tenant-manager.js   Tenant CRUD + PM2 lifecycle
│   ├── cli.js              Per-tenant CLI
│   └── admin-cli.js        Agency admin CLI
├── portal/
│   ├── src/server.js       Express upload portal
│   └── public/index.html   Customer-facing upload UI
├── tenants/                Created at runtime — gitignored
│   ├── registry.json
│   └── {locationId}/
│       ├── .env
│       ├── data/
│       │   ├── session.json
│       │   ├── synced.json
│       │   └── bookings.csv
│       └── logs/
│           ├── sync.log
│           └── pm2.log
└── setup.sh
```

## Environment Variables

See [.env.example](.env.example) for full documentation.

### Per-Tenant (in `tenants/{id}/.env`)

| Variable | Default | Description |
|---|---|---|
| `GHL_API_KEY` | — | GHL V2 API key |
| `GHL_LOCATION_ID` | — | GHL Location ID |
| `GHL_API_BASE` | `https://services.leadconnectorhq.com` | GHL API base URL |
| `GHL_TAG_SOURCE` | `eversports` | Contact tag applied to all synced contacts |
| `GHL_TAG_CLASS_NAME` | `true` | Also tag with slugified class name |
| `GHL_TAG_CUSTOMER_GROUP` | `true` | Also tag with customer group |
| `EVERSPORTS_BASE_URL` | `https://app.eversportsmanager.com` | Scrape target |
| `SYNC_DAYS_BACK` | `1` | Days to sync per cron run |
| `CRON_SCHEDULE` | `0 6-22 * * 1-6` | Cron expression for daemon |
| `WARN_DAYS_BEFORE_EXPIRY` | `5` | Days before expiry to send webhook |
| `STATE_FILE` | `./data/synced.json` | Dedup state file path |
| `CSV_BACKUP` | `./data/bookings.csv` | CSV backup file path |
| `LOG_FILE` | `./logs/sync.log` | Log file path |
| `HEADLESS` | `true` | Run Playwright headless |
| `NOTIFY_WEBHOOK_URL` | — | Webhook URL for expiry alerts |

### Portal-Level (root `.env`)

| Variable | Default | Description |
|---|---|---|
| `PORTAL_PORT` | `3000` | HTTP port for the upload portal |
| `AGENCY_NAME` | `Sync Portal` | Displayed in portal UI |
| `PORTAL_URL` | — | Public URL (used in next-steps output) |

## Security Notes

- `GHL_API_KEY` is **masked in all log output**
- Tenant secrets are stored as **SHA-256 hashes only** — never plaintext
- Portal uses Helmet CSP + rate limiting (10 req / 15 min / IP)
- Each tenant's data is fully isolated under `tenants/{locationId}/`
- No cross-tenant data access is possible at the code level

## Oracle Cloud Firewall

Oracle Cloud uses `iptables` rules (not ufw). `setup.sh` handles this automatically. If you need to open ports manually:

```bash
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 3000 -j ACCEPT
netfilter-persistent save
```

Also open port 3000 in the OCI Security List:
**OCI Console → Networking → VCN → Security Lists → Add Ingress Rule**
- Source CIDR: `0.0.0.0/0`
- Protocol: TCP
- Port: 3000
