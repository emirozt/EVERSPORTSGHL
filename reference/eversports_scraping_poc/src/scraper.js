import { chromium } from 'playwright';
import path from 'path';
import { mkdirSync } from 'fs';
import { getSessionStatus, applySessionToContext } from './session.js';

const USER_AGENT =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

/* ── helpers ──────────────────────────────────────────────────────────────── */

async function safeScreenshot(page, tenantDir, label) {
  try {
    const logsDir = path.join(tenantDir, 'logs');
    mkdirSync(logsDir, { recursive: true });
    await page.screenshot({ path: path.join(logsDir, `error-${label}-${Date.now()}.png`) });
  } catch {}
}

/**
 * Parse the semicolon-delimited CSV that Eversports exports.
 * Returns an array of plain objects keyed by the header row.
 */
function parseCsv(csvText) {
  const lines = csvText.trim().split('\n').filter(Boolean);
  if (lines.length < 2) return [];
  const clean = (s) => (s || '').trim().replace(/^"|"$/g, '');
  const headers = lines[0].split(';').map(clean);
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const values = lines[i].split(';').map(clean);
    const row = {};
    headers.forEach((h, idx) => { row[h] = values[idx] ?? ''; });
    rows.push(row);
  }
  return rows;
}

/**
 * Detect the Eversports companyId (e.g. "Yneu3U") and facilityId (e.g. 78034)
 * by navigating to the admin root and calling the facilities API.
 * Throws if detection fails.
 */
async function detectFacilityInfo(context, baseURL, logger) {
  // Try to get the company short ID from the URL after navigating to the manager root.
  // Some installs redirect; for those that don't we fall back to the env var.
  const companyIdEnv = process.env.EVERSPORTS_COMPANY_ID || null;

  let companyId = companyIdEnv;

  if (!companyId) {
    const page = await context.newPage();
    try {
      // Try a path that Eversports redirects to /admin/{id}/... when logged in
      await page.goto(`${baseURL}/manager`, { timeout: 20000, waitUntil: 'load' }).catch(() => {});
      await page.waitForTimeout(3000);
      const url = page.url();
      const m = url.match(/\/admin\/([^/?#]+)/);
      if (m) companyId = m[1];
    } finally {
      await page.close();
    }
  }

  if (!companyId) {
    throw new Error(
      'Cannot detect Eversports company ID. ' +
      'Set EVERSPORTS_COMPANY_ID in your tenant .env file ' +
      '(it is the short code in your Eversports URL, e.g. "Yneu3U").'
    );
  }

  // Fetch the numeric facility ID from the facilities API
  const res = await context.request.get(
    `${baseURL}/api/admin-facilities?facilityShortId=${companyId}`,
    { timeout: 15000 }
  );
  if (res.status() !== 200) {
    throw new Error(`Facilities API returned ${res.status()} for companyId=${companyId}`);
  }
  const data = await res.json();
  const facilityId = data?.facilities?.[0]?.id;
  if (!facilityId) {
    throw new Error(`No facility found for companyId=${companyId}`);
  }

  logger.info('Facility info detected', { companyId, facilityId });
  return { companyId, facilityId };
}

/**
 * Scrape all sessions for a single date and return booking records.
 */
async function scrapeDate(context, page, dateStr, companyId, facilityId, baseURL, tenantDir, logger) {
  const allBookings = [];
  const dateUrl = `${baseURL}/admin/${companyId}/classes?date=${dateStr}`;

  try {
    await page.goto(dateUrl, { timeout: 25000, waitUntil: 'networkidle' });
  } catch (err) {
    logger.error('Navigation to date page failed', { dateStr, error: err.message });
    await safeScreenshot(page, tenantDir, `date-${dateStr}`);
    return allBookings;
  }
  await page.waitForTimeout(1500);

  // Extract session metadata from data-eventsession JSON attributes
  const sessionDataList = await page.evaluate(() =>
    Array.from(document.querySelectorAll('tr.js_quick-data[data-eventsession]'))
      .map(row => {
        try { return JSON.parse(row.getAttribute('data-eventsession')); } catch { return null; }
      })
      .filter(Boolean)
  );

  logger.info('Sessions found for date', { date: dateStr, total: sessionDataList.length });

  for (const sd of sessionDataList) {
    // Skip cancelled sessions and sessions with no participants
    if (sd.eventSessionCancelled) continue;
    if (!sd.sessionParticipantsCount || sd.sessionParticipantsCount === 0) continue;

    const classInfo = {
      classId:    String(sd.eventSessionId),
      classTitle: sd.eventName || '',
      classDate:  sd.startDate  || dateStr,
      classTime:  sd.startTime  || '',
    };

    logger.info('Fetching participants via export API', {
      classId:      classInfo.classId,
      title:        classInfo.classTitle,
      participants: sd.sessionParticipantsCount,
    });

    try {
      const exportUrl =
        `${baseURL}/api/event/participant/list/download` +
        `?facilityId=${facilityId}&sessionId=${sd.eventSessionId}`;

      const exportRes = await context.request.get(exportUrl, { timeout: 20000 });

      if (exportRes.status() !== 200) {
        logger.warn('Export API returned non-200', {
          classId: classInfo.classId,
          status:  exportRes.status(),
        });
        continue;
      }

      const csvText = (await exportRes.body()).toString('utf8');
      const rows    = parseCsv(csvText);

      for (const row of rows) {
        const email = (row['E-Mail-Adresse'] || '').toLowerCase().trim();
        if (!email || !email.includes('@')) continue;

        allBookings.push({
          classId:       classInfo.classId,
          classTitle:    classInfo.classTitle,
          classDate:     classInfo.classDate,
          classTime:     classInfo.classTime,
          firstName:     (row['Vorname']         || '').trim(),
          lastName:      (row['Nachname']         || '').trim(),
          email,
          phone:         (row['Telefonnummer']    || '').trim(),
          customerGroup: (row['Clubgroup name']   || '').trim(),
          registeredOn:  '',
          ticketType:    (row['Produkt']          || '').trim(),
          presenceStatus: 'unknown',
        });
      }

      logger.info('Participants parsed', {
        classId: classInfo.classId,
        count:   rows.length,
      });
    } catch (err) {
      logger.error('Failed to fetch/parse participants', {
        classId: classInfo.classId,
        error:   err.message,
      });
    }
  }

  return allBookings;
}

/* ── public API ───────────────────────────────────────────────────────────── */

export async function scrapeClasses(options) {
  const { sessionFile, baseURL, dates, headless, logger, tenantDir } = options;

  const sessionStatus = getSessionStatus(sessionFile);
  if (sessionStatus.status === 'expired' || sessionStatus.status === 'missing') {
    throw new Error(`Session is ${sessionStatus.status} — cannot scrape`);
  }

  const browser = await chromium.launch({ headless: headless !== false });
  let allBookings = [];

  try {
    const context = await browser.newContext({ userAgent: USER_AGENT });
    await applySessionToContext(context, sessionFile);

    // Auto-detect company / facility identifiers
    const { companyId, facilityId } = await detectFacilityInfo(context, baseURL, logger);

    const page = await context.newPage();

    // Verify session is live
    try {
      await page.goto(`${baseURL}/admin/${companyId}/classes`, { timeout: 20000 });
    } catch (err) {
      await safeScreenshot(page, tenantDir, 'session-check');
      throw new Error(`Navigation failed: ${err.message}`);
    }
    if (page.url().includes('/login')) {
      throw new Error('Session rejected — redirected to login page');
    }

    logger.info('Session verified, starting scrape', { dates, companyId, facilityId });

    for (const dateStr of dates) {
      logger.info('Scraping date', { date: dateStr });
      try {
        const dateBookings = await scrapeDate(
          context, page, dateStr, companyId, facilityId, baseURL, tenantDir, logger
        );
        allBookings.push(...dateBookings);
        logger.info('Date scrape complete', { date: dateStr, count: dateBookings.length });
      } catch (dateErr) {
        logger.error('Error scraping date', { date: dateStr, error: dateErr.message });
      }
    }

    await context.close();
  } finally {
    await browser.close();
  }

  logger.info('Scrape complete', { totalBookings: allBookings.length });
  return allBookings;
}
