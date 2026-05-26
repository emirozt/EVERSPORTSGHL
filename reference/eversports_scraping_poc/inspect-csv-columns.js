#!/usr/bin/env node
import { readFileSync } from 'fs';
import path from 'path';
import { createLogger } from './src/logger.js';
import { chromium } from 'playwright';
import { getSessionStatus, applySessionToContext } from './src/session.js';
import { format } from 'date-fns';

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

  const baseURL = process.env.EVERSPORTS_BASE_URL || 'https://app.eversportsmanager.com';
  const companyId = process.env.EVERSPORTS_COMPANY_ID;
  const tenantDir = process.env.TENANT_DIR || '.';
  const sessionFile = path.join(tenantDir, 'data', 'session.json');

  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    await applySessionToContext(context, sessionFile);

    // Fetch facilities
    const res = await context.request.get(
      `${baseURL}/api/admin-facilities?facilityShortId=${companyId}`,
      { timeout: 15000 }
    );
    const data = await res.json();
    const facilityId = data?.facilities?.[0]?.id;

    console.log(`Facility ID: ${facilityId}\n`);

    // Get a sample class to inspect
    const page = await context.newPage();
    const dateStr = format(new Date(), 'yyyy-MM-dd');
    await page.goto(`${baseURL}/admin/${companyId}/classes?date=${dateStr}`, { timeout: 25000, waitUntil: 'networkidle' });
    await page.waitForTimeout(1500);

    const sessionDataList = await page.evaluate(() =>
      Array.from(document.querySelectorAll('tr.js_quick-data[data-eventsession]'))
        .map(row => {
          try { return JSON.parse(row.getAttribute('data-eventsession')); } catch { return null; }
        })
        .filter(Boolean)
    );

    if (sessionDataList.length === 0) {
      console.log('No sessions found for today. Trying yesterday...');
      const yesterday = format(new Date(Date.now() - 86400000), 'yyyy-MM-dd');
      await page.goto(`${baseURL}/admin/${companyId}/classes?date=${yesterday}`, { timeout: 25000 });
      await page.waitForTimeout(1500);
      
      const prevData = await page.evaluate(() =>
        Array.from(document.querySelectorAll('tr.js_quick-data[data-eventsession]'))
          .map(row => {
            try { return JSON.parse(row.getAttribute('data-eventsession')); } catch { return null; }
          })
          .filter(Boolean)
      );
      
      if (prevData.length === 0) {
        console.log('No sessions found. Exiting.');
        return;
      }
      sessionDataList.push(...prevData);
    }

    // Get first non-cancelled session with participants
    const sessionToCheck = sessionDataList.find(sd => 
      !sd.eventSessionCancelled && sd.sessionParticipantsCount > 0
    );

    if (!sessionToCheck) {
      console.log('No valid sessions found.');
      return;
    }

    console.log(`Sample session: ${sessionToCheck.eventName}\n`);
    console.log(`Fetching CSV from: /api/event/participant/list/download`);
    console.log(`Parameters: facilityId=${facilityId}, sessionId=${sessionToCheck.eventSessionId}\n`);

    const exportRes = await context.request.get(
      `${baseURL}/api/event/participant/list/download?facilityId=${facilityId}&sessionId=${sessionToCheck.eventSessionId}`,
      { timeout: 20000 }
    );

    const csvText = (await exportRes.body()).toString('utf8');
    const lines = csvText.trim().split('\n');
    
    if (lines.length > 0) {
      const clean = (s) => (s || '').trim().replace(/^"|"$/g, '');
      const headers = lines[0].split(';').map(clean);
      
      console.log(`📋 CSV Columns (${headers.length} total):`);
      console.log('────────────────────────────────────────────\n');
      for (let i = 0; i < headers.length; i++) {
        console.log(`${i + 1}. ${headers[i]}`);
      }
      
      console.log('\n────────────────────────────────────────────');
      console.log(`\nSample row values:`);
      if (lines.length > 1) {
        const values = lines[1].split(';').map(clean);
        for (let i = 0; i < Math.min(headers.length, values.length); i++) {
          console.log(`${headers[i]}: "${values[i]}"`);
        }
      }
    }

  } finally {
    await browser.close();
  }
}

main().catch(err => {
  console.error('Error:', err.message);
  process.exit(1);
});
