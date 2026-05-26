import { readFileSync, writeFileSync, existsSync, mkdirSync, rmSync } from 'fs';
import path from 'path';
import { createHash } from 'crypto';
import { execSync } from 'child_process';
import { fileURLToPath } from 'url';
import { importFromCookieJSON } from './session.js';

const TENANTS_DIR = fileURLToPath(new URL('../tenants/', import.meta.url));
const REGISTRY_FILE = path.join(TENANTS_DIR, 'registry.json');

function loadRegistry() {
  if (!existsSync(REGISTRY_FILE)) return [];
  try {
    return JSON.parse(readFileSync(REGISTRY_FILE, 'utf8'));
  } catch {
    return [];
  }
}

function saveRegistry(entries) {
  mkdirSync(TENANTS_DIR, { recursive: true });
  writeFileSync(REGISTRY_FILE, JSON.stringify(entries, null, 2), 'utf8');
}

export function hashSecret(secret) {
  return createHash('sha256').update(secret).digest('hex');
}

function tenantDir(locationId) {
  return path.join(TENANTS_DIR, locationId);
}

function pm2ProcessName(locationId) {
  return `eversync-${locationId}`;
}

function pm2Exec(args) {
  try {
    return execSync(`pm2 ${args}`, { stdio: 'pipe' }).toString();
  } catch (err) {
    return err.stdout ? err.stdout.toString() : '';
  }
}

export function provisionTenant(opts) {
  const {
    locationId,
    ghlKey,
    label,
    secretHash,
    notifyWebhook,
    syncDaysBack = '1',
    cronSchedule = '0 6-22 * * 1-6',
    warnDays = '5',
  } = opts;

  const dir = tenantDir(locationId);
  const dataDir = path.join(dir, 'data');
  const logsDir = path.join(dir, 'logs');

  mkdirSync(dataDir, { recursive: true });
  mkdirSync(logsDir, { recursive: true });

  const envContent = [
    `GHL_API_KEY=${ghlKey}`,
    `GHL_LOCATION_ID=${locationId}`,
    `GHL_API_BASE=${process.env.GHL_API_BASE || 'https://services.leadconnectorhq.com'}`,
    `GHL_TAG_SOURCE=eversports`,
    `GHL_TAG_CLASS_NAME=true`,
    `GHL_TAG_CUSTOMER_GROUP=true`,
    `EVERSPORTS_BASE_URL=${process.env.EVERSPORTS_BASE_URL || 'https://app.eversportsmanager.com'}`,
    `SYNC_DAYS_BACK=${syncDaysBack}`,
    `CRON_SCHEDULE=${cronSchedule}`,
    `WARN_DAYS_BEFORE_EXPIRY=${warnDays}`,
    `STATE_FILE=${path.join(dataDir, 'synced.json')}`,
    `CSV_BACKUP=${path.join(dataDir, 'bookings.csv')}`,
    `LOG_FILE=${path.join(logsDir, 'sync.log')}`,
    `LOG_LEVEL=info`,
    `HEADLESS=true`,
    `NOTIFY_WEBHOOK_URL=${notifyWebhook || ''}`,
    `TENANT_DIR=${dir}`,
  ].join('\n');

  writeFileSync(path.join(dir, '.env'), envContent, 'utf8');

  const registry = loadRegistry();
  const existing = registry.findIndex((r) => r.locationId === locationId);
  const entry = {
    locationId,
    label: label || locationId,
    secretHash,
    notifyWebhook: notifyWebhook || '',
    createdAt: new Date().toISOString(),
    active: true,
  };

  if (existing >= 0) {
    registry[existing] = entry;
  } else {
    registry.push(entry);
  }
  saveRegistry(registry);
  return entry;
}

export function validateCredentials(locationId, secret) {
  const registry = loadRegistry();
  const entry = registry.find((r) => r.locationId === locationId);
  if (!entry) return false;
  return entry.secretHash === hashSecret(secret);
}

export function saveTenantSession(locationId, cookieJSON) {
  const dir = tenantDir(locationId);
  const sessionFile = path.join(dir, 'data', 'session.json');
  mkdirSync(path.dirname(sessionFile), { recursive: true });
  return importFromCookieJSON(cookieJSON, sessionFile);
}

export function startTenantDaemon(locationId) {
  const dir = tenantDir(locationId);
  const envFile = path.join(dir, '.env');
  const cliPath = new URL('./cli.js', import.meta.url).pathname;
  const name = pm2ProcessName(locationId);

  pm2Exec(`delete ${name}`);
  pm2Exec(`start "${cliPath}" --name "${name}" -- daemon --env-file "${envFile}"`);
  pm2Exec('save');
}

export function stopTenantDaemon(locationId) {
  const name = pm2ProcessName(locationId);
  pm2Exec(`stop "${name}"`);
}

export function getTenantDaemonStatus(locationId) {
  const name = pm2ProcessName(locationId);
  try {
    const output = execSync('pm2 jlist', { stdio: 'pipe' }).toString();
    const list = JSON.parse(output);
    const proc = list.find((p) => p.name === name);
    if (!proc) return 'not found';
    return proc.pm2_env?.status || 'stopped';
  } catch {
    return 'not found';
  }
}

export function deprovisionTenant(locationId) {
  stopTenantDaemon(locationId);

  const dir = tenantDir(locationId);
  if (existsSync(dir)) {
    rmSync(dir, { recursive: true, force: true });
  }

  const registry = loadRegistry();
  const updated = registry.filter((r) => r.locationId !== locationId);
  saveRegistry(updated);
}

export function listTenants() {
  const registry = loadRegistry();
  return registry.map((entry) => ({
    ...entry,
    daemonStatus: getTenantDaemonStatus(entry.locationId),
  }));
}

export function getTenant(locationId) {
  const registry = loadRegistry();
  return registry.find((r) => r.locationId === locationId) || null;
}

export function updateTenantSecretHash(locationId, newSecretHash) {
  const registry = loadRegistry();
  const idx = registry.findIndex((r) => r.locationId === locationId);
  if (idx < 0) throw new Error(`Tenant not found: ${locationId}`);
  registry[idx].secretHash = newSecretHash;
  saveRegistry(registry);
}
