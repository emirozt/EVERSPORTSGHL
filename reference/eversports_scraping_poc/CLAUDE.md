# Eversports → GoHighLevel CRM Sync Daemon

**Project Location:** `/Users/emirsmacbook/Documents/ScaleUp Works/01 PROJECTS/01 EVERSPORTS CRM CONNECTOR`

**Status:** ✅ Production-ready (632 contacts synced, zero errors)

---

## Project Overview

A **multi-tenant Node.js daemon** that automatically syncs fitness class participant data from Eversports Manager to GoHighLevel (GHL) CRM. Each tenant (gym/studio) operates independently with isolated configuration, session management, and deduplication state.

**Core Workflow:**
1. User uploads browser cookies from Eversports via web portal
2. Daemon scrapes participant data via Eversports CSV export API
3. Deduplicates against synced.json (fingerprint: `${email}||${classId}||${classDate}`)
4. Upserts contacts to GHL with tags (source, class name, customer group, package type)
5. Logs all operations and maintains CSV backup
6. Runs on configurable cron schedule (default: hourly during business hours)

---

## Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Runtime** | Node.js | 20.20.2 |
| **Module System** | ES6 Modules | (`"type": "module"`) |
| **Browser Automation** | Playwright | Latest |
| **Web Server** | Express.js | 4.x |
| **API Clients** | Native fetch + axios-like patterns | Built-in |
| **Process Manager** | PM2 | For daemon scheduling |
| **Logging** | Custom JSON logger | Per-tenant files |
| **File Upload** | Multer | Memory storage, 1MB limit |
| **Security** | Helmet | CSP, rate limiting |
| **Date Utils** | date-fns | Formatting, calculations |
| **Task Scheduling** | node-cron | Cron expressions |

---

## Architecture

### Multi-Tenant Model

Each tenant gets a dedicated directory structure:
```
tenants/{locationId}/
├── .env                      # Per-tenant environment config
├── data/
│   ├── session.json         # Playwright cookies (30-day expiry)
│   ├── synced.json          # Dedup state {email||classId||classDate: true}
│   └── bookings.csv         # Backup of all synced bookings
└── logs/
    └── sync.log             # Structured JSON logs
```

**Tenant Isolation:**
- Separate PM2 process per tenant (named `eversync-{locationId}`)
- Independent environment variables and API credentials
- No shared state between tenants
- Automatic daemon start/stop via tenant-manager.js

### Core Modules

**src/cli.js** (1,261 lines)
- Main CLI entry point with 6 commands
- `sync` — One-time sync (supports `--date`, `--days`, `--dry-run`)
- `daemon` — Background cron process (reads `CRON_SCHEDULE`, `SYNC_DAYS_BACK`)
- `status` — Print session & sync state
- `verify-session` — Test Eversports auth
- `clear-state` — Reset dedup state

**src/scraper.js** (244 lines)
- **Auto-detection:** Finds `companyId` from URL if not in env
- **Facility lookup:** Queries `/api/admin-facilities?facilityShortId={companyId}` for numeric `facilityId`
- **Session validation:** Checks session is live before scraping
- **Date iteration:** Scrapes multiple dates in parallel pages
- **CSV parsing:** Parses semicolon-delimited Eversports export
- **Column extraction:** 
  - `Vorname` → firstName
  - `Nachname` → lastName
  - `E-Mail-Adresse` → email (required, filtered for @)
  - `Telefonnummer` → phone
  - `Clubgroup name` → customerGroup
  - `Produkt` → ticketType (captures package/card name)
- **Returns:** Array of booking objects with classId, classTitle, classDate, classTime, contact info

**src/session.js** (134 lines)
- **Cookie import:** `importFromCookieJSON()` — parses exported cookies, normalizes domain/sameSite, sets 30-day expiry
- **Status check:** `getSessionStatus()` — checks expiry, warns if < 5 days left (configurable)
- **Browser context:** `applySessionToContext()` — injects cookies into Playwright context
- **Live verification:** `verifySessionLive()` — navigates to `/admin/{companyId}/classes`, checks not redirected to /login

**src/ghl.js** (147 lines)
- **Upsert logic:** `syncBookingsToGHL()` — creates or updates contacts
- **Deduplication:** Checks against synced.json fingerprint
- **Tagging:** Adds tags for source, class name, customer group, package
- **Notes:** Appends class & date info if enabled (not in dry-run)
- **Batch handling:** Processes with error tracking

**src/state.js** (69 lines)
- Manages `synced.json` dedup state
- Fingerprint format: `${email}||${classId}||${classDate}`
- Prevents duplicate syncs of same class booking

**src/csv.js** (26 lines)
- Appends booking rows to tenant's `bookings.csv`
- Used for audit trail and offline analysis

**src/logger.js** (54 lines)
- Structured JSON logging per tenant
- Supports log levels: info, warn, error
- Writes to tenant-specific `.log` files

**src/tenant-manager.js** (166 lines)
- Multi-tenant daemon orchestration
- Lists, validates, and manages tenant configurations
- Starts/stops PM2 processes per tenant
- Credential validation via SHA-256 hashing

**src/admin-cli.js** (151 lines)
- Tenant CRUD operations
- Add/list/remove tenants
- Generate credentials and share tokens

**portal/src/server.js** (175 lines)
- Express server with Helmet CSP (`script-src 'self'`)
- **Rate limiting:** 10 uploads per 15 minutes per IP
- **POST /upload** — Accepts cookies JSON, validates credentials, saves session, starts daemon
- **GET /status/:locationId** — Returns session/daemon status
- Listens on `process.env.PORTAL_PORT` (default: 3000)

**portal/public/portal.js** (External JS file)
- Compliant with CSP `script-src 'self'` (no inline scripts)
- Drop zone for JSON file selection
- Paste textarea for copy-pasted cookies
- FormData multipart upload to `/upload`
- 15-second fetch timeout with AbortController
- Error/success message display

---

## Environment Variables

**Per-Tenant (.env file)** — Required:
```
# GoHighLevel API
GHL_API_KEY=pit-{secret}                    # OAuth token
GHL_LOCATION_ID={locationId}                # Tenant ID in GHL
GHL_API_BASE=https://services.leadconnectorhq.com

# Eversports
EVERSPORTS_BASE_URL=https://app.eversportsmanager.com
EVERSPORTS_COMPANY_ID={companyId}           # e.g. "Yneu3U" (auto-detected if missing)

# Sync Configuration
SYNC_DAYS_BACK=1                            # Days to sync per cron tick
CRON_SCHEDULE=0 6-22 * * 1-6                # Mon-Sat, 6am-10pm
WARN_DAYS_BEFORE_EXPIRY=5                   # Alert threshold for session expiry

# Paths
STATE_FILE=/path/to/data/synced.json
CSV_BACKUP=/path/to/data/bookings.csv
LOG_FILE=/path/to/logs/sync.log
TENANT_DIR=/path/to/tenants/{locationId}

# Options
LOG_LEVEL=info                              # info | warn | error
HEADLESS=true                               # true = no browser window
NOTIFY_WEBHOOK_URL=                         # Optional: Slack/Discord webhook for alerts
```

**Portal Server (.env or ENV vars):**
```
PORTAL_PORT=3000
PORTAL_SECRET={shared-secret}               # Used for credential validation
```

---

## Key Workflows

### 1. Local Development / Testing

**Verify session is valid:**
```bash
node src/cli.js --env-file tenants/{locationId}/.env verify-session
# Output: ✓ Session is valid and accepted by Eversports.
```

**Dry-run sync (preview without writing to GHL):**
```bash
node src/cli.js --env-file tenants/{locationId}/.env sync --days 3 --dry-run
# Output: Scraped: 996, Created: 237, Updated: 0, Skipped: 759, Errors: 0 (DRY RUN)
```

**Sync single date:**
```bash
node src/cli.js --env-file tenants/{locationId}/.env sync --date 2026-05-24
```

**Check tenant status:**
```bash
node src/cli.js --env-file tenants/{locationId}/.env status
# Output: Location ID, Session status, Days left, Last run, Synced total
```

**Clear dedup state (re-sync all):**
```bash
node src/cli.js --env-file tenants/{locationId}/.env clear-state
```

### 2. Portal Setup

**Start portal server:**
```bash
npm run portal  # or: node portal/src/server.js
# Listening on http://localhost:3000
```

**Upload flow:**
1. User navigates to portal
2. Enters Location ID and API secret
3. Exports cookies from Eversports (Chrome DevTools → Application → Cookies → app.eversportsmanager.com)
4. Pastes JSON or drops file in portal
5. Clicks "Upload & Activate"
6. Portal validates credentials, saves session, starts daemon
7. Returns success with session expiry date

### 3. Daemon Deployment

**Start daemon for a tenant:**
```bash
node src/cli.js --env-file tenants/{locationId}/.env daemon
# Daemon starting { schedule: '0 6-22 * * 1-6', locationId: 'sBbY9ixZw1ixwLpdvv1G' }
# Listens for cron ticks...
```

**Manage via PM2 (if using PM2):**
```bash
pm2 start src/cli.js --name "eversync-{locationId}" -- daemon --env-file tenants/{locationId}/.env
pm2 logs eversync-{locationId}
pm2 stop eversync-{locationId}
```

**Scheduled via systemd (on Linux servers):**
```ini
# /etc/systemd/system/eversync.service
[Unit]
Description=Eversports CRM Sync Daemon
After=network.target

[Service]
Type=simple
User=eversync
WorkingDirectory=/opt/eversync
ExecStart=/usr/bin/node src/cli.js daemon --env-file tenants/{locationId}/.env
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4. Analysis & Reporting

**Filter contacts by package type:**
```bash
node analyze-trial-packages.js tenants/{locationId}/.env
# Lists all product types, counts, and identifies "trial" packages
```

**Export trial contacts to CSV:**
```bash
node unique-trial-contacts.js tenants/{locationId}/.env
# Outputs: trial-contacts.csv with name, email, phone, class count
```

**Query GHL for synced contacts:**
```bash
# Use GHL API v2 REST
curl -H "Authorization: Bearer {GHL_API_KEY}" \
  https://services.leadconnectorhq.com/v2/contacts/?locationId={locationId}
```

---

## API Integration Points

### Eversports API

**Facilities (Auto-detect company ID):**
```
GET /api/admin-facilities?facilityShortId={companyId}
Response: { facilities: [{ id: 78034, ... }] }
```

**Participant Export (CSV):**
```
GET /api/event/participant/list/download?facilityId={facilityId}&sessionId={sessionId}
Response: Text CSV (semicolon-delimited, German headers)
Columns: Kundennummer, Nachname, Vorname, E-Mail-Adresse, Clubgroup name, 
         Marketing Kommunikation, Telefonnummer, Alter, Geburtsdatum, Land,
         PLZ, city, Strasse, Kommentar, Notiz, Warnung, Klasse, Optionen,
         Texte, Produkt, Gesamtpreis, Zahlungsstatus, Aggregator
```

**Session Metadata (HTML data attribute):**
```
HTML: <tr class="js_quick-data" data-eventsession="{...}">
JSON: { eventSessionId, eventName, startDate, startTime, 
        sessionParticipantsCount, eventSessionCancelled }
```

### GoHighLevel API v2

**Upsert Contact:**
```
POST /v2/contacts/upsert/?locationId={locationId}
Body: {
  firstName, lastName, email, phone,
  tags: ["eversports", "Class Name", "Customer Group", "Package Type"],
  customFields: { /* optional */ },
  source: "Eversports",
  notes: "Class: ..., Date: ..., TicketType: ..."
}
Response: { contact: { id, email, ... }, status: "created|updated" }
```

---

## Database & State Management

### synced.json (Deduplication State)
```json
{
  "user@example.com||classId123||2026-05-24": true,
  "user2@example.com||classId456||2026-05-25": true
}
```
- Prevents duplicate syncs of same class booking
- Key format: `${email}||${classId}||${classDate}`
- Cleared with `--clear-state` to re-sync

### session.json (Playwright Cookies)
```json
{
  "cookies": [
    {
      "name": "session_id",
      "value": "...",
      "domain": ".eversportsmanager.com",
      "path": "/",
      "expires": 1719259200,
      "httpOnly": true,
      "secure": true,
      "sameSite": "Lax"
    }
  ],
  "origins": [],
  "capturedAt": "2026-05-24T10:00:00.000Z",
  "expiresAt": "2026-06-23T10:00:00.000Z",
  "captureMethod": "cookie-editor"
}
```
- 30-day validity from import date
- Playwright injects into browser context
- Verified live before each scrape

### bookings.csv (Audit Trail)
```csv
classId,classTitle,classDate,classTime,firstName,lastName,email,phone,customerGroup,registeredOn,ticketType,presenceStatus
85192553,Weekend Reformer Group Class,2026-05-23,19:00,John,Doe,john@example.com,+49...,Extern,,10er Karte-Gruppe,unknown
```

---

## Common Issues & Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| **Session rejected — redirected to login** | Cookies expired or invalid | Re-upload fresh cookies via portal |
| **Cannot detect Eversports company ID** | URL doesn't redirect to `/admin/{id}/` | Set `EVERSPORTS_COMPANY_ID` in .env manually |
| **No facilities found for companyId** | Wrong company ID or API access denied | Verify companyId matches Eversports URL (e.g., "Yneu3U") |
| **CSV parsing errors** | Unexpected column headers or format | Check Eversports export format; may have changed |
| **0 bookings scraped** | Classes cancelled, no participants, or wrong dates | Check date range, class status; try `--dry-run` to inspect |
| **GHL sync fails with 401** | Invalid or expired API key | Regenerate `GHL_API_KEY` in GoHighLevel dashboard |
| **Port 3000 already in use** | Another process using the port | Change `PORTAL_PORT` or kill existing process |
| **CSP violation in portal** | Inline scripts blocked by Helmet | Ensure portal.js is external file with `<script src="/portal.js">` |
| **Rate limit exceeded on /upload** | Too many uploads in 15 min window | Wait 15 minutes or increase `max` in express-rate-limit config |

---

## Performance Considerations

- **Scraping speed:** ~1-2 seconds per class (API call + CSV parse)
- **Sync speed:** ~50-100ms per contact (GHL API)
- **Memory:** Minimal; Playwright context closed after each session
- **Disk:** CSV backups grow ~1-10KB per sync depending on volume
- **Network:** Requires stable internet; Eversports API timeouts set to 20s

---

## Security Notes

1. **API Keys:** Never commit `.env` files; use `.env.example` template
2. **Cookies:** Session cookies stored plaintext in session.json; keep tenants directory private
3. **Credentials:** Portal validates via SHA-256 hash comparison (never plaintext)
4. **CSP:** Portal enforces `script-src 'self'` to prevent XSS
5. **Rate Limiting:** Portal limits uploads to 10/15min per IP
6. **Webhook Auth:** NOTIFY_WEBHOOK_URL can include auth in URL if needed

---

## File Manifest

| File | Purpose | Lines |
|------|---------|-------|
| `src/cli.js` | Main CLI interface | 261 |
| `src/scraper.js` | Eversports scraper + CSV parser | 244 |
| `src/session.js` | Cookie session management | 134 |
| `src/ghl.js` | GHL API sync logic | 147 |
| `src/csv.js` | CSV backup writer | 26 |
| `src/logger.js` | JSON logger | 54 |
| `src/state.js` | Dedup state manager | 69 |
| `src/tenant-manager.js` | Multi-tenant orchestration | 166 |
| `src/admin-cli.js` | Tenant admin CLI | 151 |
| `portal/src/server.js` | Express portal server | 175 |
| `portal/public/index.html` | Portal form UI | ~150 |
| `portal/public/portal.js` | Client-side JS (CSP-compliant) | ~200 |
| `package.json` | Dependencies & scripts | ~40 |
| `CLAUDE.md` | This documentation | — |

---

## Getting Started Checklist

- [ ] Clone/download project
- [ ] Run `npm install`
- [ ] Create first tenant: `node src/admin-cli.js add-tenant --id {locationId} --label "Studio Name"`
- [ ] Copy `.env.example` to `tenants/{locationId}/.env`
- [ ] Fill in Eversports & GHL credentials
- [ ] Start portal: `npm run portal` (or `node portal/src/server.js`)
- [ ] Navigate to `http://localhost:3000`
- [ ] Export cookies from Eversports, upload via portal
- [ ] Verify session: `node src/cli.js --env-file tenants/{locationId}/.env verify-session`
- [ ] Test sync: `node src/cli.js --env-file tenants/{locationId}/.env sync --date today --dry-run`
- [ ] Deploy daemon: `node src/cli.js --env-file tenants/{locationId}/.env daemon`

---

## Contact & Support

**Project Owner:** Emir (emiroztrk@gmail.com)  
**Last Updated:** 2026-05-24  
**Version:** 1.0 (Production)  
**Status:** 632 contacts synced, zero errors ✅

---

*This CLAUDE.md serves as the primary reference for all Cowork sessions. Update it as the project evolves.*
