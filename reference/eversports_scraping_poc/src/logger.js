import winston from 'winston';
import path from 'path';
import { mkdirSync } from 'fs';

const { createLogger: winstonCreateLogger, format, transports } = winston;
const { combine, timestamp, json, printf, colorize, errors } = format;

const API_KEY_PATTERN = /Bearer\s+[A-Za-z0-9\-_\.]+/g;

const maskSecrets = format((info) => {
  const str = JSON.stringify(info);
  const masked = str.replace(API_KEY_PATTERN, 'Bearer [REDACTED]');
  return JSON.parse(masked);
});

const consoleFormat = printf(({ level, message, timestamp: ts, ...meta }) => {
  const metaStr = Object.keys(meta).length ? ` ${JSON.stringify(meta)}` : '';
  return `${ts} [${level}] ${message}${metaStr}`;
});

export function createLogger(tenantId, logFile, logLevel = 'info') {
  const logDir = path.dirname(logFile);
  mkdirSync(logDir, { recursive: true });

  return winstonCreateLogger({
    level: logLevel,
    format: combine(
      maskSecrets(),
      errors({ stack: true }),
      timestamp()
    ),
    defaultMeta: { tenantId },
    transports: [
      new transports.File({
        filename: logFile,
        format: combine(maskSecrets(), timestamp(), json()),
      }),
      new transports.Console({
        format: combine(
          maskSecrets(),
          colorize(),
          timestamp({ format: 'HH:mm:ss' }),
          consoleFormat
        ),
      }),
    ],
  });
}

export const defaultLogger = createLogger(
  'admin',
  process.env.LOG_FILE || './logs/admin.log',
  process.env.LOG_LEVEL || 'info'
);
