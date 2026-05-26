import { createObjectCsvWriter } from 'csv-writer';
import { mkdirSync } from 'fs';
import path from 'path';

const COLUMNS = [
  { id: 'classDate', title: 'classDate' },
  { id: 'classTime', title: 'classTime' },
  { id: 'classTitle', title: 'classTitle' },
  { id: 'firstName', title: 'firstName' },
  { id: 'lastName', title: 'lastName' },
  { id: 'email', title: 'email' },
  { id: 'phone', title: 'phone' },
  { id: 'customerGroup', title: 'customerGroup' },
  { id: 'registeredOn', title: 'registeredOn' },
  { id: 'ticketType', title: 'ticketType' },
  { id: 'presenceStatus', title: 'presenceStatus' },
  { id: 'syncedAt', title: 'syncedAt' },
];

export async function appendBookingsToCSV(bookings, csvFile) {
  if (!bookings || bookings.length === 0) return;

  mkdirSync(path.dirname(csvFile), { recursive: true });

  const writer = createObjectCsvWriter({
    path: csvFile,
    header: COLUMNS,
    append: true,
  });

  const syncedAt = new Date().toISOString();
  const records = bookings.map((b) => ({ ...b, syncedAt }));

  await writer.writeRecords(records);
}
