import { readFileSync, writeFileSync, rmSync, existsSync, mkdirSync } from 'fs';
import path from 'path';

export function fingerprint(booking) {
  const { email, classId, classDate } = booking;
  return `${email}||${classId}||${classDate}`;
}

function loadState(stateFile) {
  if (!existsSync(stateFile)) return { synced: {}, lastRun: null };
  try {
    return JSON.parse(readFileSync(stateFile, 'utf8'));
  } catch {
    return { synced: {}, lastRun: null };
  }
}

function saveState(stateFile, state) {
  mkdirSync(path.dirname(stateFile), { recursive: true });
  writeFileSync(stateFile, JSON.stringify(state, null, 2), 'utf8');
}

export function hasSynced(stateFile, fp) {
  const state = loadState(stateFile);
  return Boolean(state.synced && state.synced[fp]);
}

export function markSyncedBatch(stateFile, fps) {
  const state = loadState(stateFile);
  if (!state.synced) state.synced = {};
  const now = new Date().toISOString();
  for (const fp of fps) {
    state.synced[fp] = now;
  }
  state.lastRun = now;
  saveState(stateFile, state);
}

export function getLastRun(stateFile) {
  return loadState(stateFile).lastRun || null;
}

export function getSyncedCount(stateFile) {
  const state = loadState(stateFile);
  return state.synced ? Object.keys(state.synced).length : 0;
}

export function clearState(stateFile) {
  if (existsSync(stateFile)) {
    rmSync(stateFile);
  }
}
