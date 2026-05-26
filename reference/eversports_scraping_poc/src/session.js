import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import path from 'path';
import { addDays, differenceInDays, parseISO } from 'date-fns';
import { chromium } from 'playwright';

const WARN_DAYS = parseInt(process.env.WARN_DAYS_BEFORE_EXPIRY || '5', 10);

const SAMESITE_MAP = {
  strict: 'Strict',
  lax: 'Lax',
  none: 'None',
  no_restriction: 'None',
  unspecified: 'Lax',
};

function normaliseSameSite(raw) {
  if (!raw) return 'Lax';
  return SAMESITE_MAP[raw.toLowerCase()] || 'Lax';
}

function normaliseDomain(domain) {
  if (!domain) return '';
  return domain.startsWith('.') ? domain : `.${domain}`;
}

export function importFromCookieJSON(rawJSON, sessionFile) {
  let cookies;
  try {
    cookies = typeof rawJSON === 'string' ? JSON.parse(rawJSON) : rawJSON;
  } catch (err) {
    throw new Error(`Failed to parse cookie JSON: ${err.message}`);
  }

  if (!Array.isArray(cookies)) {
    throw new Error('Cookie JSON must be an array');
  }

  const normalisedCookies = cookies.map((c) => ({
    name: c.name,
    value: c.value,
    domain: normaliseDomain(c.domain),
    path: c.path || '/',
    expires: c.expirationDate || c.expires || -1,
    httpOnly: Boolean(c.httpOnly),
    secure: Boolean(c.secure),
    sameSite: normaliseSameSite(c.sameSite),
  }));

  const capturedAt = new Date().toISOString();
  const expiresAt = addDays(new Date(), 30).toISOString();

  const session = {
    cookies: normalisedCookies,
    origins: [],
    capturedAt,
    expiresAt,
    captureMethod: 'cookie-editor',
  };

  mkdirSync(path.dirname(sessionFile), { recursive: true });
  writeFileSync(sessionFile, JSON.stringify(session, null, 2), 'utf8');
  return session;
}

export function sessionExists(sessionFile) {
  return existsSync(sessionFile);
}

export function getSessionStatus(sessionFile) {
  if (!existsSync(sessionFile)) {
    return { status: 'missing', daysLeft: null, expiresAt: null };
  }

  let session;
  try {
    session = JSON.parse(readFileSync(sessionFile, 'utf8'));
  } catch {
    return { status: 'missing', daysLeft: null, expiresAt: null };
  }

  const expiresAt = session.expiresAt;
  if (!expiresAt) {
    return { status: 'missing', daysLeft: null, expiresAt: null };
  }

  const now = new Date();
  const expiry = parseISO(expiresAt);
  const daysLeft = differenceInDays(expiry, now);

  if (daysLeft < 0) {
    return { status: 'expired', daysLeft, expiresAt };
  }
  if (daysLeft <= WARN_DAYS) {
    return { status: 'expiring-soon', daysLeft, expiresAt };
  }
  return { status: 'valid', daysLeft, expiresAt };
}

export async function applySessionToContext(context, sessionFile) {
  const session = JSON.parse(readFileSync(sessionFile, 'utf8'));
  if (session.cookies && session.cookies.length > 0) {
    await context.addCookies(session.cookies);
  }
  if (session.origins && session.origins.length > 0) {
    await context.storageState({ path: undefined });
  }
}

export async function verifySessionLive(sessionFile, baseURL, companyId) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    await applySessionToContext(context, sessionFile);
    const page = await context.newPage();

    // Navigate to the correct admin URL if we know the company ID,
    // otherwise fall back to the root (which may redirect to the admin area).
    const targetUrl = companyId
      ? `${baseURL}/admin/${companyId}/classes`
      : baseURL;

    await page.goto(targetUrl, { timeout: 25000, waitUntil: 'load' });
    await page.waitForTimeout(2000);
    const url = page.url();

    // Authenticated if not redirected to login
    return !url.includes('/login') && !url.includes('/auth/login');
  } catch {
    return false;
  } finally {
    await browser.close();
  }
}
