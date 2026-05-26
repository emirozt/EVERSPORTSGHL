#!/usr/bin/env bash
set -euo pipefail

# ─── Eversync One-Command Server Setup ───────────────────────────────────────
# Target: Ubuntu 22.04 (Oracle Cloud Always Free ARM/AMD or any vanilla VPS)
# Run as: sudo bash setup.sh
# Optional: set DOMAIN=yourdomain.com for nginx + Let's Encrypt TLS
# ─────────────────────────────────────────────────────────────────────────────

INSTALL_DIR="/opt/eversync"
PORTAL_PORT="${PORTAL_PORT:-3000}"
DOMAIN="${DOMAIN:-}"
SUDO_USER="${SUDO_USER:-$(logname 2>/dev/null || echo ubuntu)}"

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Eversync Setup — Ubuntu 22.04"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 1. System update ──────────────────────────────────────────────────────────
echo "► Updating system packages…"
apt-get update -qq
apt-get upgrade -y -qq

# ── 2. Node.js 20 via NodeSource ──────────────────────────────────────────────
echo "► Installing Node.js 20…"
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1
apt-get install -y nodejs >/dev/null

echo "  Node: $(node --version)  npm: $(npm --version)"

# ── 3. Playwright system dependencies ─────────────────────────────────────────
echo "► Installing Playwright Chromium system dependencies…"
cd "$INSTALL_DIR" 2>/dev/null || true
npx --yes playwright install-deps chromium >/dev/null 2>&1 || true

# ── 4. PM2 global install + systemd startup ───────────────────────────────────
echo "► Installing PM2 globally…"
npm install -g pm2 >/dev/null

HOME_DIR="/home/${SUDO_USER}"
if [ "$SUDO_USER" = "root" ]; then
  HOME_DIR="/root"
fi

echo "► Configuring PM2 systemd startup for user: ${SUDO_USER}…"
pm2 startup systemd -u "$SUDO_USER" --hp "$HOME_DIR" | tail -1 | bash || true

# ── 5. Copy project to /opt/eversync ─────────────────────────────────────────
echo "► Installing project to ${INSTALL_DIR}…"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
  cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR/"
fi

cd "$INSTALL_DIR"
echo "► Installing npm dependencies…"
npm install --omit=dev >/dev/null

# ── 6. Playwright Chromium browser binary ─────────────────────────────────────
echo "► Installing Playwright Chromium browser…"
npx playwright install chromium >/dev/null 2>&1

# ── 7. Copy .env.example → .env (no overwrite) ────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "► Created .env from .env.example (edit it before starting the portal)"
else
  echo "► .env already exists — skipping copy"
fi

# ── 8. Start portal via PM2 ───────────────────────────────────────────────────
echo "► Starting eversync-portal via PM2…"
pm2 delete eversync-portal 2>/dev/null || true
pm2 start "$INSTALL_DIR/portal/src/server.js" \
  --name eversync-portal \
  --env "$INSTALL_DIR/.env" \
  --log "$INSTALL_DIR/logs/portal.log" \
  --time
pm2 save

# ── 9. Oracle Cloud iptables firewall rules ───────────────────────────────────
echo "► Opening ports in iptables (Oracle Cloud)…"
if command -v iptables >/dev/null 2>&1; then
  iptables -I INPUT 6 -m state --state NEW -p tcp --dport 3000 -j ACCEPT 2>/dev/null || true
  iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT 2>/dev/null || true
  iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT 2>/dev/null || true

  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save 2>/dev/null || true
  elif command -v iptables-save >/dev/null 2>&1; then
    iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
  fi
  echo "  iptables rules applied."
else
  echo "  iptables not found — skipping (not Oracle Cloud?)"
fi

# ── 10. Optional: nginx + certbot TLS ─────────────────────────────────────────
if [ -n "$DOMAIN" ]; then
  echo "► DOMAIN=$DOMAIN — setting up nginx reverse proxy + Let's Encrypt TLS…"
  apt-get install -y nginx certbot python3-certbot-nginx >/dev/null

  NGINX_CONF="/etc/nginx/sites-available/eversync"
  cat > "$NGINX_CONF" <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass         http://127.0.0.1:${PORTAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection 'upgrade';
        proxy_set_header   Host \$host;
        proxy_cache_bypass \$http_upgrade;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        client_max_body_size 2M;
    }
}
NGINX

  ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/eversync
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx

  certbot --nginx \
    -d "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    -m "admin@${DOMAIN}" \
    --redirect || echo "  certbot failed — check DNS and try manually: certbot --nginx -d $DOMAIN"

  echo "  TLS configured for https://${DOMAIN}"
fi

# ── Done ───────────────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "<server-ip>")
PORTAL_URL="http://${SERVER_IP}:${PORTAL_PORT}"
if [ -n "$DOMAIN" ]; then
  PORTAL_URL="https://${DOMAIN}"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  ✅ Setup complete. Next steps:"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  1. Open port ${PORTAL_PORT} in OCI Security List (if not done yet):"
echo "     OCI Console → Networking → VCN → Security Lists → Add Ingress Rule"
echo "     Source CIDR: 0.0.0.0/0  |  Protocol: TCP  |  Port: ${PORTAL_PORT}"
echo ""
echo "  2. Provision your first tenant:"
echo "     node ${INSTALL_DIR}/src/admin-cli.js add-tenant \\"
echo "       --location-id <GHL_LOCATION_ID> \\"
echo "       --ghl-key     <GHL_API_KEY> \\"
echo "       --label       \"Studio Name\""
echo ""
echo "  3. Share the portal URL with your customer:"
echo "     ${PORTAL_URL}"
echo ""
echo "  4. Check PM2 status:"
echo "     pm2 list"
echo ""

# ──────────────────────────────────────────────────────────────────────────────
# ALTERNATIVE: Zero-infrastructure deployment (no VPS)
# ──────────────────────────────────────────────────────────────────────────────
#
# ── GitHub Actions as scheduler ───────────────────────────────────────────────
# Create .github/workflows/sync.yml:
#
# name: Eversports Sync
# on:
#   schedule:
#     - cron: "0 6-22 * * 1-6"
#   workflow_dispatch:
# jobs:
#   sync:
#     runs-on: ubuntu-latest
#     steps:
#       - uses: actions/checkout@v4
#       - uses: actions/setup-node@v4
#         with:
#           node-version: '20'
#       - run: npm ci
#       - run: npx playwright install chromium --with-deps
#       - name: Run sync
#         run: node src/cli.js sync --days 1
#         env:
#           GHL_API_KEY:      ${{ secrets.GHL_API_KEY }}
#           GHL_LOCATION_ID:  ${{ secrets.GHL_LOCATION_ID }}
#           STATE_FILE:       ./data/synced.json
#           CSV_BACKUP:       ./data/bookings.csv
#           LOG_FILE:         ./logs/sync.log
#           EVERSPORTS_BASE_URL: https://app.eversportsmanager.com
#           TENANT_DIR:       .
#
# LIMITATION: GitHub Actions has no persistent filesystem between runs.
# The session.json (cookies) and synced.json (dedup state) are lost after each
# run. For persistent operation you must either:
#   a) Upload session.json as a GitHub Actions artifact and download it at run start
#   b) Store session.json in a GitHub secret (base64-encoded) and restore it
#   c) Use a paid VPS (recommended for production)
#
# ── Render.com for portal ─────────────────────────────────────────────────────
# 1. Push this repo to GitHub.
# 2. Create a new Web Service on render.com, connect your repo.
# 3. Set Build Command:  npm install && npx playwright install chromium --with-deps
# 4. Set Start Command:  npm run start:portal
# 5. Add env vars in the Render dashboard (PORTAL_PORT, etc.)
# Note: Render free tier sleeps after 15 min inactivity.
#       Use the $7/mo Starter plan for an always-on portal.
# ──────────────────────────────────────────────────────────────────────────────
