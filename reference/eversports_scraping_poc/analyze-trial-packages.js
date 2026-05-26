#!/usr/bin/env node
import { readFileSync, writeFileSync, existsSync } from 'fs';
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

  logger.info('Starting analysis', { dates });
  console.log('Scraping 7 days of data...');

  const bookings = await scrapeClasses({ sessionFile, baseURL, dates, headless: true, logger, tenantDir });

  // Group by product type and collect contact info
  const productMap = {};
  for (const booking of bookings) {
    const product = booking.ticketType || 'Unknown';
    if (!productMap[product]) {
      productMap[product] = [];
    }
    productMap[product].push({
      name: `${booking.firstName} ${booking.lastName}`,
      email: booking.email,
      classTitle: booking.classTitle,
      classDate: booking.classDate,
    });
  }

  // Sort by count descending
  const sorted = Object.entries(productMap).sort((a, b) => b[1].length - a[1].length);

  console.log('\n── Product Type Summary ──────────────────────────');
  for (const [product, contacts] of sorted) {
    console.log(`${product}: ${contacts.length} contacts`);
  }

  // Detect potential trial packages
  const trialKeywords = ['trial', 'probe', 'test', 'schnupperstunde', 'gratis', 'kostenlos', 'kostenloses', 'free'];
  const potentialTrials = sorted.filter(([product]) =>
    trialKeywords.some(kw => product.toLowerCase().includes(kw))
  );

  if (potentialTrials.length > 0) {
    console.log('\n── Potential Trial Packages ──────────────────────');
    for (const [product, contacts] of potentialTrials) {
      console.log(`\n${product} (${contacts.length} contacts):`);
      for (const contact of contacts.slice(0, 5)) {
        console.log(`  - ${contact.name} (${contact.email})`);
      }
      if (contacts.length > 5) {
        console.log(`  ... and ${contacts.length - 5} more`);
      }
    }
  } else {
    console.log('\n── No obvious trial packages found ──────────────────');
    console.log('Packages containing keywords: trial, probe, test, schnupperstunde, gratis, kostenlos, free');
    console.log('\nPlease review the product list above and let me know which should be filtered.');
  }

  console.log('──────────────────────────────────────────────────\n');
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
