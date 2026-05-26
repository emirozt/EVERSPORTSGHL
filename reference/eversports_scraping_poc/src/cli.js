#!/usr/bin/env node
import { readFileSync, existsSync } from 'fs';
import path from 'path';
import { Command } from 'commander';
import { subDays, format } from 'date-fns';
import cron from 'node-cron';
import { createLogger } from './logger.js';
import { clearState, getLastRun, getSyncedCount } from './state.js';
import { getSessionStatus, verifySessionLive } from './session.js';
import { scrapeClasses } from './scraper.js';
import { syncBookingsToGHL } from './ghl.js';
import { appendBookingsToCSV } from './csv.js';

function loadEnv(envFile) {
  const file = envFile || process.env.TENANT_ENV_FILE;
  if (file && existsSync(file)) {
    const lines = readFileSync(file, 'utf8').split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx < 0) continue;
      const key = trimmed.slice(0, eqIdx).trim();
      const val = trimmed.slice(eqIdx + 1).trim();
      if (!process.env[key]) process.env[key] = val;
    }
  }
}

function requireEnv(name) {
  if (!process.env[name]) {
    console.error(`Error: ${name} is not set. Use --env-file or set TENANT_ENV_FILE.`);
    process.exit(1);
  }
  return process.env[name];
}

function buildDates(opts) {
  if (opts.days) {
    const n = parseInt(opts.days, 10);
    const dates = [];
    for (let i = n - 1; i >= 0; i--) {
      dates.push(format(subDays(new Date(), i), 'yyyy-MM-dd'));
    }
    return dates;
  }
  const d = opts.date === 'today' || !opts.date ? format(new Date(), 'yyyy-MM-dd') : opts.date;
  return [d];
}

async function fireWebhook(url, payload) {
  if (!url) return;
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    console.error('Webhook POST failed:', err.message);
  }
}

async function runSync(opts, envFile) {
  loadEnv(envFile);
  const locationId = requireEnv('GHL_LOCATION_ID');
  const apiKey = requireEnv('GHL_API_KEY');
  const baseURL = process.env.EVERSPORTS_BASE_URL || 'https://app.eversportsmanager.com';
  const stateFile = process.env.STATE_FILE || './data/synced.json';
  const csvFile = process.env.CSV_BACKUP || './data/bookings.csv';
  const logFile = process.env.LOG_FILE || './logs/sync.log';
  const tenantDir = process.env.TENANT_DIR || '.';
  const sessionFile = path.join(tenantDir, 'data', 'session.json');
  const headless = process.env.HEADLESS !== 'false';

  const logger = createLogger(locationId, logFile, process.env.LOG_LEVEL || 'info');

  if (opts.clearState) {
    clearState(stateFile);
    logger.info('State cleared');
  }

  const dates = buildDates(opts);
  logger.info('Starting sync', { dates, dryRun: opts.dryRun });

  let bookings;
  try {
    bookings = await scrapeClasses({ sessionFile, baseURL, dates, headless, logger, tenantDir });
  } catch (err) {
    logger.error('Scrape failed', { error: err.message });
    process.exit(1);
  }

  const config = {
    locationId,
    apiKey,
    tagSource: process.env.GHL_TAG_SOURCE || 'eversports',
    tagClassName: process.env.GHL_TAG_CLASS_NAME === 'true',
    tagCustomerGroup: process.env.GHL_TAG_CUSTOMER_GROUP === 'true',
  };

  const results = await syncBookingsToGHL(bookings, stateFile, config, {
    dryRun: opts.dryRun,
    noNotes: opts.noNotes,
    noDedup: opts.noDedup,
  });

  if (!opts.dryRun) {
    await appendBookingsToCSV(bookings, csvFile);
  }

  console.log('\n── Sync Summary ──────────────────────────');
  console.log(`  Scraped:  ${bookings.length}`);
  console.log(`  Created:  ${results.created}`);
  console.log(`  Updated:  ${results.updated}`);
  console.log(`  Skipped:  ${results.skipped}`);
  console.log(`  Errors:   ${results.errors}`);
  if (opts.dryRun) console.log('  (DRY RUN — nothing written to GHL)');
  console.log('──────────────────────────────────────────\n');

  logger.info('Sync complete', results);
}

const program = new Command();

program
  .name('eversync')
  .option('--env-file <path>', 'Path to tenant .env file');

program
  .command('sync')
  .description('Scrape Eversports and sync bookings to GHL')
  .option('--date <YYYY-MM-DD|today>', 'Sync a single date (default: today)')
  .option('--days <n>', 'Sync last N days')
  .option('--dry-run', 'Scrape but do not write to GHL')
  .option('--no-notes', 'Skip addNote calls')
  .option('--no-dedup', 'Ignore dedup state')
  .option('--clear-state', 'Delete synced.json before running')
  .action(async (opts) => {
    const envFile = program.opts().envFile;
    await runSync(opts, envFile);
  });

program
  .command('daemon')
  .description('Start the cron-scheduled sync daemon')
  .action(() => {
    const envFile = program.opts().envFile;
    loadEnv(envFile);

    const locationId = requireEnv('GHL_LOCATION_ID');
    const logFile = process.env.LOG_FILE || './logs/sync.log';
    const tenantDir = process.env.TENANT_DIR || '.';
    const sessionFile = path.join(tenantDir, 'data', 'session.json');
    const schedule = process.env.CRON_SCHEDULE || '0 6-22 * * 1-6';
    const syncDaysBack = parseInt(process.env.SYNC_DAYS_BACK || '1', 10);
    const notifyWebhookUrl = process.env.NOTIFY_WEBHOOK_URL;

    const logger = createLogger(locationId, logFile, process.env.LOG_LEVEL || 'info');
    logger.info('Daemon starting', { schedule, locationId });

    cron.schedule(schedule, async () => {
      logger.info('Cron tick — checking session');
      const sessionStatus = getSessionStatus(sessionFile);

      if (sessionStatus.status === 'missing') {
        logger.error('Session missing — skipping run');
        return;
      }

      if (sessionStatus.status === 'expired') {
        logger.warn('Session expired — skipping run', sessionStatus);
        await fireWebhook(notifyWebhookUrl, {
          event: 'session_expired',
          locationId,
          daysLeft: sessionStatus.daysLeft,
          expiresAt: sessionStatus.expiresAt,
        });
        return;
      }

      if (sessionStatus.status === 'expiring-soon') {
        logger.warn('Session expiring soon', sessionStatus);
        await fireWebhook(notifyWebhookUrl, {
          event: 'session_expiring_soon',
          locationId,
          daysLeft: sessionStatus.daysLeft,
          expiresAt: sessionStatus.expiresAt,
        });
      }

      try {
        await runSync({ days: String(syncDaysBack) }, envFile);
      } catch (err) {
        logger.error('Daemon sync run failed', { error: err.message });
      }
    });
  });

program
  .command('status')
  .description('Print session and sync status')
  .action(() => {
    const envFile = program.opts().envFile;
    loadEnv(envFile);

    const locationId = requireEnv('GHL_LOCATION_ID');
    const tenantDir = process.env.TENANT_DIR || '.';
    const sessionFile = path.join(tenantDir, 'data', 'session.json');
    const stateFile = process.env.STATE_FILE || './data/synced.json';

    const sessionStatus = getSessionStatus(sessionFile);
    const lastRun = getLastRun(stateFile);
    const syncedCount = getSyncedCount(stateFile);

    console.log('\n── Tenant Status ─────────────────────────');
    console.log(`  Location ID:   ${locationId}`);
    console.log(`  Session:       ${sessionStatus.status}`);
    console.log(`  Days left:     ${sessionStatus.daysLeft ?? 'N/A'}`);
    console.log(`  Expires at:    ${sessionStatus.expiresAt ?? 'N/A'}`);
    console.log(`  Last run:      ${lastRun ?? 'Never'}`);
    console.log(`  Synced total:  ${syncedCount}`);
    console.log('──────────────────────────────────────────\n');
  });

program
  .command('verify-session')
  .description('Verify the session is alive against Eversports')
  .action(async () => {
    const envFile = program.opts().envFile;
    loadEnv(envFile);

    const baseURL = process.env.EVERSPORTS_BASE_URL || 'https://app.eversportsmanager.com';
    const tenantDir = process.env.TENANT_DIR || '.';
    const sessionFile = path.join(tenantDir, 'data', 'session.json');

    const companyId = process.env.EVERSPORTS_COMPANY_ID || undefined;
    console.log('Verifying session live…');
    const alive = await verifySessionLive(sessionFile, baseURL, companyId);
    if (alive) {
      console.log('✓ Session is valid and accepted by Eversports.');
    } else {
      console.log('✗ Session is invalid or expired — redirect to login detected.');
      process.exit(1);
    }
  });

program
  .command('clear-state')
  .description('Delete the dedup state file (synced.json)')
  .action(() => {
    const envFile = program.opts().envFile;
    loadEnv(envFile);

    const stateFile = process.env.STATE_FILE || './data/synced.json';
    clearState(stateFile);
    console.log(`State cleared: ${stateFile}`);
  });

program.parse(process.argv);
