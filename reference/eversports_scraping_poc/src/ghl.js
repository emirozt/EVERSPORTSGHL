import { hasSynced, markSyncedBatch, fingerprint } from './state.js';

const DEFAULT_BASE = 'https://services.leadconnectorhq.com';
const API_VERSION = '2021-07-28';
const RETRY_DELAY_MS = 1000;
const MAX_RETRIES = 3;
const BETWEEN_CALLS_MS = 120;

function getBase() {
  return (process.env.GHL_API_BASE || DEFAULT_BASE).replace(/\/$/, '');
}

function headers(apiKey) {
  return {
    Authorization: `Bearer ${apiKey}`,
    Version: API_VERSION,
    'Content-Type': 'application/json',
  };
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchWithRetry(url, opts, retries = 0) {
  const res = await fetch(url, opts);
  if (res.status === 429 && retries < MAX_RETRIES) {
    await sleep(RETRY_DELAY_MS);
    return fetchWithRetry(url, opts, retries + 1);
  }
  return res;
}

export async function testConnection(locationId, apiKey) {
  try {
    const res = await fetchWithRetry(`${getBase()}/locations/${locationId}`, {
      method: 'GET',
      headers: headers(apiKey),
    });
    return res.status === 200;
  } catch {
    return false;
  }
}

function slugify(str) {
  return str
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}

function buildTags(booking, config) {
  const tags = [];
  if (config.tagSource) tags.push(config.tagSource);
  if (config.tagClassName && booking.classTitle) tags.push(slugify(booking.classTitle));
  if (config.tagCustomerGroup && booking.customerGroup) tags.push(slugify(booking.customerGroup));
  return tags;
}

export async function upsertContact(booking, config) {
  const { locationId, apiKey } = config;
  const tags = buildTags(booking, config);

  const body = {
    locationId,
    email: booking.email,
    firstName: booking.firstName,
    lastName: booking.lastName,
    phone: booking.phone || undefined,
    tags,
    source: config.tagSource || 'eversports',
    customFields: [
      { key: 'eversports_last_class', field_value: `${booking.classTitle} (${booking.classDate})` },
      { key: 'eversports_customer_group', field_value: booking.customerGroup || '' },
    ],
  };

  const res = await fetchWithRetry(`${getBase()}/contacts/upsert`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`upsertContact failed (${res.status}): ${text}`);
  }

  const data = await res.json();
  return { id: data.contact?.id || data.id, isNew: res.status === 201 };
}

export async function addNote(contactId, text, apiKey) {
  const res = await fetchWithRetry(`${getBase()}/contacts/${contactId}/notes`, {
    method: 'POST',
    headers: headers(apiKey),
    body: JSON.stringify({ body: text }),
  });

  if (!res.ok) {
    const errText = await res.text().catch(() => '');
    throw new Error(`addNote failed (${res.status}): ${errText}`);
  }
  return res.json();
}

export async function syncBookingsToGHL(bookings, stateFile, config, options = {}) {
  const { dryRun = false, noNotes = false, noDedup = false } = options;
  const results = { created: 0, updated: 0, skipped: 0, errors: 0 };
  const syncedFps = [];

  for (const booking of bookings) {
    const fp = fingerprint(booking);

    if (!noDedup && hasSynced(stateFile, fp)) {
      results.skipped++;
      continue;
    }

    if (dryRun) {
      results.created++;
      continue;
    }

    try {
      const { id, isNew } = await upsertContact(booking, config);
      if (isNew) results.created++;
      else results.updated++;

      if (!noNotes && id) {
        const noteText =
          `Eversports booking: ${booking.classTitle} on ${booking.classDate} at ${booking.classTime}` +
          (booking.ticketType ? ` | Ticket: ${booking.ticketType}` : '') +
          (booking.presenceStatus && booking.presenceStatus !== 'unknown' ? ` | Presence: ${booking.presenceStatus}` : '') +
          (booking.registeredOn ? ` | Registered: ${booking.registeredOn}` : '');
        await addNote(id, noteText, config.apiKey).catch(() => {});
      }

      syncedFps.push(fp);
    } catch (err) {
      results.errors++;
    }

    await sleep(BETWEEN_CALLS_MS);
  }

  if (syncedFps.length > 0) {
    markSyncedBatch(stateFile, syncedFps);
  }

  return results;
}
