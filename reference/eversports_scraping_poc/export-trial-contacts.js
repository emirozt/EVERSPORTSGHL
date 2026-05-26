#!/usr/bin/env node
import { readFileSync } from 'fs';
import path from 'path';
import { createLogger } from './src/logger.js';
import { scrapeClasses } from './src/scraper.js';
import { subDays, format } from 'date-fns';

async function main() {
  const envFile = process.argv[2] || 'tenants/sBbY9ixZw1ixwLpdvv1G/.env';

  // Load env
  const lines = readFileSync(envFile, 'utf8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx < 0) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const val = trimmed.slice(eqIdx + 1).trim();
    if (!process.env[key]) process.env[key] = val;
  }

  const locationId = process.env.GHL_LOCATION_ID;
  const baseURL = process.env.EVERSPORTS_BASE_URL || 'https://app.eversportsmanager.com';
  const tenantDir = process.env.TENANT_DIR || '.';
  const sessionFile = path.join(tenantDir, 'data', 'session.json');
  const logFile = path.join(tenantDir, 'logs', 'analysis.log');

  const logger = createLogger(locationId, logFile, 'info');

  // Build last 7 days
  const dates = [];
  for (let i = 6; i >= 0; i--) {
    dates.push(format(subDays(new Date(), i), 'yyyy-MM-dd'));
  }

  const bookings = await scrapeClasses({ sessionFile, baseURL, dates, headless: true, logger, tenantDir });

  // Find trial contacts
  const trialKeyword = '3 Trial Cards';
  const trialContacts = bookings.filter(b => b.ticketType && b.ticketType.includes(trialKeyword));

  console.log(`\n── Trial Package Contacts (${trialContacts.length} total) ──────────────────────────\n`);

  // Sort by last name
  trialContacts.sort((a, b) => {
    const lastA = a.lastName || '';
    const lastB = b.lastName || '';
    return lastA.localeCompare(lastB);
  });

  for (let i = 0; i < trialContacts.length; i++) {
    const c = trialContacts[i];
    const fullName = `${c.firstName || ''} ${c.lastName || ''}`.trim();
    console.log(`${i + 1}. ${fullName}`);
    console.log(`   Email: ${c.email}`);
    console.log(`   Phone: ${c.phone || 'N/A'}`);
    console.log(`   Class: ${c.classTitle}`);
    console.log(`   Date: ${c.classDate}`);
    console.log('');
  }

  console.log(`──────────────────────────────────────────────────\n`);
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
