#!/usr/bin/env node
import { Command } from 'commander';
import { nanoid } from 'nanoid';
import chalk from 'chalk';
import {
  provisionTenant,
  validateCredentials,
  listTenants,
  getTenant,
  startTenantDaemon,
  stopTenantDaemon,
  deprovisionTenant,
  updateTenantSecretHash,
  hashSecret,
} from './tenant-manager.js';
import { getSessionStatus } from './session.js';
import path from 'path';
import { fileURLToPath } from 'url';

const TENANTS_DIR = fileURLToPath(new URL('../tenants/', import.meta.url));

function tenantSessionFile(locationId) {
  return path.join(TENANTS_DIR, locationId, 'data', 'session.json');
}

function pad(str, len) {
  return String(str).padEnd(len);
}

const program = new Command();
program.name('eversync-admin').description('Eversports → GHL Sync — Agency Admin CLI');

program
  .command('add-tenant')
  .description('Provision a new tenant')
  .requiredOption('--location-id <id>', 'GHL Location ID')
  .requiredOption('--ghl-key <key>', 'GHL API Key')
  .option('--label <name>', 'Human-readable studio name')
  .option('--secret <secret>', 'Portal secret (auto-generated if omitted)')
  .option('--notify-webhook <url>', 'Webhook URL for session expiry alerts')
  .action((opts) => {
    const secret = opts.secret || nanoid(32);
    const generated = !opts.secret;
    const sHash = hashSecret(secret);

    provisionTenant({
      locationId: opts.locationId,
      ghlKey: opts.ghlKey,
      label: opts.label,
      secretHash: sHash,
      notifyWebhook: opts.notifyWebhook,
    });

    startTenantDaemon(opts.locationId);

    console.log(chalk.green(`\n✓ Tenant provisioned: ${opts.label || opts.locationId}`));
    console.log(`  Location ID: ${opts.locationId}`);
    if (generated) {
      console.log(chalk.yellow('\n  ⚠ Portal Secret (save this — it cannot be recovered):'));
      console.log(chalk.bold(`  ${secret}\n`));
    } else {
      console.log('  Secret: [as provided]');
    }
    console.log('  Daemon started via PM2.\n');
  });

program
  .command('list-tenants')
  .description('List all provisioned tenants')
  .action(() => {
    const tenants = listTenants();
    if (tenants.length === 0) {
      console.log('No tenants provisioned yet.');
      return;
    }

    const header = [
      pad('Label', 20),
      pad('Location ID', 22),
      pad('Daemon', 10),
      pad('Session Expiry', 22),
      pad('Active', 6),
    ].join(' | ');
    console.log('\n' + chalk.bold(header));
    console.log('─'.repeat(header.length));

    for (const t of tenants) {
      const sessionFile = tenantSessionFile(t.locationId);
      const sess = getSessionStatus(sessionFile);
      const expiryStr = sess.expiresAt
        ? `${sess.status} (${sess.daysLeft}d)`
        : sess.status;

      const expiry =
        sess.daysLeft !== null && sess.daysLeft <= 5
          ? chalk.yellow(pad(expiryStr, 22))
          : pad(expiryStr, 22);

      const daemon =
        t.daemonStatus === 'online'
          ? chalk.green(pad(t.daemonStatus, 10))
          : chalk.red(pad(t.daemonStatus, 10));

      const row = [
        pad(t.label, 20),
        pad(t.locationId, 22),
        daemon,
        expiry,
        pad(t.active ? 'yes' : 'no', 6),
      ].join(' | ');
      console.log(row);
    }
    console.log();
  });

program
  .command('start-tenant <locationId>')
  .description('Start the PM2 daemon for a tenant')
  .action((locationId) => {
    const tenant = getTenant(locationId);
    if (!tenant) {
      console.error(chalk.red(`Tenant not found: ${locationId}`));
      process.exit(1);
    }
    startTenantDaemon(locationId);
    console.log(chalk.green(`✓ Started daemon for ${locationId}`));
  });

program
  .command('stop-tenant <locationId>')
  .description('Stop the PM2 daemon for a tenant')
  .action((locationId) => {
    const tenant = getTenant(locationId);
    if (!tenant) {
      console.error(chalk.red(`Tenant not found: ${locationId}`));
      process.exit(1);
    }
    stopTenantDaemon(locationId);
    console.log(chalk.yellow(`⏹ Stopped daemon for ${locationId}`));
  });

program
  .command('remove-tenant <locationId>')
  .description('Deprovision a tenant (irreversible)')
  .option('--confirm', 'Required confirmation flag')
  .action((locationId, opts) => {
    if (!opts.confirm) {
      console.error(chalk.red('Refusing to remove tenant without --confirm flag.'));
      process.exit(1);
    }
    const tenant = getTenant(locationId);
    if (!tenant) {
      console.error(chalk.red(`Tenant not found: ${locationId}`));
      process.exit(1);
    }
    deprovisionTenant(locationId);
    console.log(chalk.red(`✓ Tenant ${locationId} removed.`));
  });

program
  .command('rotate-secret <locationId>')
  .description('Rotate the portal upload secret for a tenant')
  .option('--secret <secret>', 'New secret (auto-generated if omitted)')
  .action((locationId, opts) => {
    const tenant = getTenant(locationId);
    if (!tenant) {
      console.error(chalk.red(`Tenant not found: ${locationId}`));
      process.exit(1);
    }
    const secret = opts.secret || nanoid(32);
    const generated = !opts.secret;
    updateTenantSecretHash(locationId, hashSecret(secret));

    console.log(chalk.green(`✓ Secret rotated for ${locationId}`));
    if (generated) {
      console.log(chalk.yellow('\n  ⚠ New Portal Secret (save this — it cannot be recovered):'));
      console.log(chalk.bold(`  ${secret}\n`));
    }
  });

program.parse(process.argv);
