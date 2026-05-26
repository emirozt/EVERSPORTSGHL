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
  const trialBookings = bookings.filter(b => b.ticketType && b.ticketType.includes(trialKeyword));

  // Deduplicate by email
  const seen = new Set();
  const uniqueTrials = [];
  
  for (const booking of trialBookings) {
    if (!seen.has(booking.email)) {
      seen.add(booking.email);
      uniqueTrials.push({
        name: `${booking.firstName} ${booking.lastName}`,
        email: booking.email,
        phone: booking.phone,
        classes: []
      });
    }
  }

  // Group classes by email
  const classMap = {};
  for (const booking of trialBookings) {
    if (!classMap[booking.email]) classMap[booking.email] = [];
    classMap[booking.email].push({
      title: booking.classTitle,
      date: booking.classDate
    });
  }

  // Attach classes to unique contacts
  for (const contact of uniqueTrials) {
    contact.classes = classMap[contact.email];
  }

  // Sort by last name
  uniqueTrials.sort((a, b) => a.name.localeCompare(b.name));

  console.log(`\n═══════════════════════════════════════════════════\n`);
  console.log(`TRIAL PACKAGE CONTACTS: ${uniqueTrials.length} unique contacts\n`);
  console.log(`Package: "3 Trial Cards-Introduction to Pilates Reformer"\n`);
  console.log(`═══════════════════════════════════════════════════\n`);

  for (let i = 0; i < uniqueTrials.length; i++) {
    const c = uniqueTrials[i];
    console.log(`${i + 1}. ${c.name}`);
    console.log(`   📧 ${c.email}`);
    console.log(`   📱 ${c.phone || 'N/A'}`);
    console.log(`   Classes: ${c.classes.length} bookings`);
    console.log('');
  }

  console.log(`═══════════════════════════════════════════════════\n`);
  console.log(`Total bookings with trial package: ${trialBookings.length}`);
  console.log(`Unique trial customers: ${uniqueTrials.length}\n`);
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
