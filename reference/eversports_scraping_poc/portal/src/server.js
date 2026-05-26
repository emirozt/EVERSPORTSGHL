import 'dotenv/config';
import express from 'express';
import multer from 'multer';
import helmet from 'helmet';
import rateLimit from 'express-rate-limit';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  validateCredentials,
  saveTenantSession,
  startTenantDaemon,
  getTenant,
  getTenantDaemonStatus,
} from '../../src/tenant-manager.js';
import { getSessionStatus } from '../../src/session.js';
import { defaultLogger as logger } from '../../src/logger.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, '../public');
const TENANTS_DIR = fileURLToPath(new URL('../../tenants/', import.meta.url));

const app = express();

app.use(
  helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        styleSrc: ["'self'", "'unsafe-inline'"],
        scriptSrc: ["'self'"],
        imgSrc: ["'self'", 'data:'],
      },
    },
  })
);

app.use(express.json());
app.use(express.static(PUBLIC_DIR));

const uploadLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 10,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many upload attempts. Please wait 15 minutes.' },
});

const storage = multer.memoryStorage();
const upload = multer({
  storage,
  limits: { fileSize: 1 * 1024 * 1024 },
  fileFilter(req, file, cb) {
    if (
      file.mimetype === 'application/json' ||
      file.mimetype === 'text/json' ||
      file.originalname.endsWith('.json')
    ) {
      cb(null, true);
    } else {
      cb(new Error('Only JSON files are accepted'));
    }
  },
});

app.post('/upload', uploadLimiter, upload.single('cookies'), async (req, res) => {
  try {
    const { locationId, secret } = req.body || {};

    if (!locationId || typeof locationId !== 'string' || !locationId.trim()) {
      return res.status(400).json({ error: 'locationId is required' });
    }
    if (!secret || typeof secret !== 'string' || !secret.trim()) {
      return res.status(400).json({ error: 'secret is required' });
    }
    if (!req.file) {
      return res.status(400).json({ error: 'Cookie JSON file is required' });
    }

    if (!validateCredentials(locationId.trim(), secret.trim())) {
      logger.warn('Invalid credentials on upload', { locationId: locationId.trim(), ip: req.ip });
      return res.status(401).json({ error: 'Invalid credentials' });
    }

    let cookies;
    try {
      cookies = JSON.parse(req.file.buffer.toString('utf8'));
    } catch {
      return res.status(400).json({ error: 'Uploaded file is not valid JSON' });
    }

    if (!Array.isArray(cookies) || cookies.length < 1) {
      return res.status(400).json({ error: 'Cookie JSON must be a non-empty array' });
    }

    const hasEversportsCookie = cookies.some(
      (c) => c.domain && c.domain.includes('eversportsmanager')
    );
    if (!hasEversportsCookie) {
      return res
        .status(400)
        .json({ error: 'No Eversports cookies found. Export cookies from app.eversportsmanager.com.' });
    }

    const session = saveTenantSession(locationId.trim(), cookies);

    try {
      startTenantDaemon(locationId.trim());
    } catch (daemonErr) {
      logger.warn('Could not start daemon after session upload', {
        locationId: locationId.trim(),
        error: daemonErr.message,
      });
    }

    logger.info('Session uploaded and daemon restarted', { locationId: locationId.trim() });
    return res.status(200).json({ success: true, expiresAt: session.expiresAt });
  } catch (err) {
    logger.error('Upload handler error', { error: err.message });
    return res.status(500).json({ error: 'Internal server error' });
  }
});

app.get('/status/:locationId', (req, res) => {
  try {
    const { locationId } = req.params;
    const authHeader = req.headers.authorization || '';
    const secret = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';

    if (!secret || !validateCredentials(locationId, secret)) {
      return res.status(401).json({ error: 'Invalid credentials' });
    }

    const tenant = getTenant(locationId);
    if (!tenant) {
      return res.status(404).json({ error: 'Tenant not found' });
    }

    const sessionFile = path.join(TENANTS_DIR, locationId, 'data', 'session.json');
    const session = getSessionStatus(sessionFile);
    const daemonStatus = getTenantDaemonStatus(locationId);

    return res.status(200).json({
      tenant: {
        locationId: tenant.locationId,
        label: tenant.label,
      },
      active: tenant.active,
      daemon: daemonStatus,
      session: {
        status: session.status,
        daysLeft: session.daysLeft,
        expiresAt: session.expiresAt,
      },
    });
  } catch (err) {
    logger.error('Status handler error', { error: err.message });
    return res.status(500).json({ error: 'Internal server error' });
  }
});

app.use((err, req, res, _next) => {
  if (err.code === 'LIMIT_FILE_SIZE') {
    return res.status(400).json({ error: 'File too large. Maximum 1 MB.' });
  }
  logger.error('Unhandled express error', { error: err.message });
  return res.status(500).json({ error: 'Internal server error' });
});

const PORT = parseInt(process.env.PORTAL_PORT || '3000', 10);
app.listen(PORT, () => {
  logger.info(`Portal listening on port ${PORT}`);
  console.log(`Eversync Portal running at http://localhost:${PORT}`);
});

export default app;
