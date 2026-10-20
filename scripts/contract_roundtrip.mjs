import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import * as validators from '../frontend/src/infra/generated/validators.js';

const root = fileURLToPath(new URL('../', import.meta.url));
const schema = JSON.parse(await fs.readFile(new URL('../frontend/src/infra/generated/contracts.schema.json', import.meta.url), 'utf8'));
const commands = Object.keys(schema.properties.requests.properties).sort();
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'htu-node-roundtrip-'));
const environment = { ...process.env, PYTHONPATH: path.join(root, 'backend/src') };
for (const key of ['USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'CODEX_HOME', 'HTU_DATA_DIR',
  'HTU_SOURCE_ROOT_CODEX', 'HTU_SOURCE_ROOT_ZCODE', 'HTU_SOURCE_ROOT_DSH']) {
  environment[key] = path.join(directory, key.toLowerCase());
  await fs.mkdir(environment[key]);
}

class Bridge {
  constructor() {
    this.process = spawn(process.argv[2] ?? 'python', ['-m', 'htu', '--headless-bridge'], {
      cwd: root, env: environment, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.pending = [];
    this.stderr = '';
    this.failure = null;
    this.exited = new Promise((resolve, reject) => {
      this.process.once('error', error => { this.fail(error); reject(error); });
      this.process.once('close', (code, signal) => {
        this.fail(new Error(`Bridge closed (${code ?? signal}): ${this.stderr}`));
        resolve(code);
      });
    });
    // Keep spawn errors handled even before close() is awaited.
    this.exited.catch(() => {});
    this.process.stderr.on('data', data => { this.stderr += data.toString(); });
    this.process.stdin.on('error', error => this.fail(error));
    this.lines = createInterface({ input: this.process.stdout });
    this.lines.on('line', line => {
      const waiter = this.pending.shift();
      if (!waiter) { this.fail(new Error(`Unexpected stdout: ${line}`)); return; }
      clearTimeout(waiter.timer);
      try { waiter.resolve(JSON.parse(line)); } catch (error) { waiter.reject(error); }
    });
  }

  fail(error) {
    this.failure ??= error;
    for (const waiter of this.pending.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
  }

  async call(command, payload = {}, expectedError) {
    if (this.failure) throw this.failure;
    const index = commands.indexOf(command);
    assert.ok(index >= 0, command);
    assert.ok(validators[`requests${index}`](payload), `Invalid request: ${command}`);
    const response = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const error = new Error(`Timed out: ${command}`);
        this.fail(error);
        this.process.kill();
      }, 10000);
      this.pending.push({ resolve, reject, timer });
      this.process.stdin.write(JSON.stringify({ command, payload }) + '\n');
    });
    const validate = validators[`responses${index}`];
    assert.ok(validate(response), `${command}: ${JSON.stringify(validate.errors)}`);
    if (expectedError) {
      assert.equal(response.ok, false);
      assert.equal(response.error.code, expectedError);
      return response;
    }
    assert.equal(response.ok, true, JSON.stringify(response));
    return response.data;
  }

  async close() {
    this.process.stdin.end();
    const timer = setTimeout(() => this.process.kill(), 10000);
    try {
      assert.equal(await this.exited, 0);
      assert.equal(this.stderr, '');
      assert.equal(this.pending.length, 0);
    } finally { clearTimeout(timer); this.lines.close(); }
  }
}

let bridge;
const write = parameters => ({ operationId: randomUUID(), ...parameters });
try {
  bridge = new Bridge();
  const hello = await bridge.call('app.hello', { clientProtocol: 1 });
  assert.deepEqual([...hello.capabilities].sort(), commands);
  await bridge.call('collection.setPaused', write({ paused: true }));
  const preferencePayload = write({ patch: { theme: 'dark', timezone: 'UTC', historyPrompted: true }, expectedRevision: hello.revisions.preferences });
  const preferences = await bridge.call('preferences.update', preferencePayload);
  const originalSources = await bridge.call('sources.list');
  const startedAt = originalSources.sources.find(source => source.harness === 'codex').startedAt;
  await fs.mkdir(path.join(directory, 'source with spaces'));
  await fs.mkdir(path.join(directory, 'next source'));
  const sourcePayload = write({ harness: 'codex', path: path.join(directory, 'source with spaces'), expectedRevision: originalSources.revision });
  const sources = await bridge.call('sources.setPath', sourcePayload);
  const created = await bridge.call('pricing.createGroup', write({ name: 'Roundtrip', expectedRevision: 0 }));
  const savePayload = write({ versionId: created.versionId, expectedRevision: created.revision,
    models: [{ modelId: 'model', input: '1.500001', cache: '0', output: '2' }] });
  const saved = await bridge.call('pricing.saveVersionPrices', savePayload);
  const switchPayload = write({ harness: 'codex', groupId: created.groupId, versionId: created.versionId, expectedRevision: saved.revision });
  const switched = await bridge.call('pricing.switchCurrent', switchPayload);
  const vendor = await bridge.call('wallet.createVendor', write({ name: 'Roundtrip Vendor', initialBalance: '-0.5' }));
  const linked = await bridge.call('wallet.linkGroups', write({ vendorId: vendor.vendorId, groupIds: [created.groupId], expectedRevision: vendor.revision }));
  const topUpPayload = write({ vendorId: vendor.vendorId, amount: '10000.123456' });
  const toppedUp = await bridge.call('wallet.topUp', topUpPayload);
  assert.equal(toppedUp.revision, linked.revision + 1);
  const calibrationPayload = write({ vendorId: vendor.vendorId, target: '8.75', expectedWalletRevision: toppedUp.revision });
  const calibrated = await bridge.call('wallet.calibrate', calibrationPayload);
  const beforeActivation = await bridge.call('pricing.catalog');
  const unused = await bridge.call('pricing.createGroup', write({ name: 'Unused', expectedRevision: beforeActivation.revision }));
  const newVersion = await bridge.call('pricing.addVersion', write({ groupId: unused.groupId, number: 2, expectedRevision: unused.revision }));
  const activationPayload = write({ groupId: unused.groupId, versionId: newVersion.versionId, expectedRevision: newVersion.revision });
  const activated = await bridge.call('pricing.activateVersion', activationPayload);
  assert.deepEqual(activated.affectedHarnesses, []);
  const prices = await bridge.call('pricing.catalog');
  assert.deepEqual(prices.current, beforeActivation.current);
  assert.equal(prices.groups.find(group => group.id === unused.groupId).versions.find(version => version.current).id, newVersion.versionId);
  const wallets = await bridge.call('wallet.list');
  const first = await bridge.call('app.sync');
  assert.equal(first.revisions.preferences, preferences.revision);
  assert.equal(first.revisions.sources, sources.revision);
  assert.equal(first.revisions.pricing, prices.revision);
  assert.equal(first.revisions.wallet, calibrated.revision);
  await bridge.close();

  bridge = new Bridge();
  const restarted = await bridge.call('app.hello', { clientProtocol: 1 });
  assert.deepEqual(restarted.revisions, first.revisions);
  assert.deepEqual(await bridge.call('preferences.get'), preferences);
  assert.deepEqual(await bridge.call('sources.list'), sources);
  assert.equal(sources.sources.find(source => source.harness === 'codex').startedAt, startedAt);
  assert.deepEqual(await bridge.call('pricing.catalog'), prices);
  assert.deepEqual(await bridge.call('wallet.list'), wallets);
  assert.equal(wallets.vendors[0].B, '8750000000000');
  assert.equal(wallets.vendors[0].R, '10000123456000000');
  assert.equal(prices.groups[0].versions[0].revisionCount, 2);
  for (const [command, payload, expected] of [
    ['preferences.update', preferencePayload, preferences], ['sources.setPath', sourcePayload, sources],
    ['pricing.saveVersionPrices', savePayload, saved], ['pricing.switchCurrent', switchPayload, switched],
    ['pricing.activateVersion', activationPayload, activated],
    ['wallet.topUp', topUpPayload, toppedUp], ['wallet.calibrate', calibrationPayload, calibrated],
  ]) assert.deepEqual(await bridge.call(command, payload), expected);
  assert.deepEqual((await bridge.call('app.sync')).revisions, first.revisions);
  const conflict = await bridge.call('preferences.update', write({ ...preferencePayload, operationId: randomUUID() }), 'PREFERENCES_REVISION_CONFLICT');
  assert.equal(conflict.error.conflictRevision, preferences.revision);
  const nextPreferences = await bridge.call('preferences.update', write({ patch: { theme: 'light' }, expectedRevision: preferences.revision }));
  assert.equal(nextPreferences.revision, preferences.revision + 1);
  const nextSources = await bridge.call('sources.setPath', write({ ...sourcePayload, path: path.join(directory, 'next source'), expectedRevision: sources.revision, operationId: randomUUID() }));
  assert.equal(nextSources.revision, sources.revision + 1);
  assert.equal(nextSources.sources.find(source => source.harness === 'codex').startedAt, startedAt);
  const nextPrices = await bridge.call('pricing.saveVersionPrices', write({ ...savePayload, models: [{ modelId: 'model', input: '3' }], expectedRevision: prices.revision, operationId: randomUUID() }));
  assert.equal(nextPrices.revision, prices.revision + 1);
  const nextTopUp = await bridge.call('wallet.topUp', write({ vendorId: vendor.vendorId, amount: '1' }));
  assert.equal(nextTopUp.revision, calibrated.revision + 1);
  assert.equal(nextTopUp.vendor.B, '9750000000000');
  const nextCalibration = await bridge.call('wallet.calibrate', write({ vendorId: vendor.vendorId, target: '9.5', expectedWalletRevision: nextTopUp.revision }));
  assert.equal(nextCalibration.revision, nextTopUp.revision + 1);
  assert.equal(nextCalibration.vendor.B, '9500000000000');
  const localPayload = write({vendorId:vendor.vendorId,R:'123000000000123',C:'1000000000000',B:'9500000000000',pastTopUpPico:'5',pastConsumptionPico:'7',expectedWalletRevision:nextCalibration.revision});
  const local = await bridge.call('wallet.setLocalTotals', localPayload);
  assert.equal(local.vendor.localWallet.R, '123000000000123');
  assert.equal(local.vendor.localWallet.B, '9500000000000');
  assert.deepEqual(await bridge.call('wallet.setLocalTotals', localPayload), local);
  const book = await bridge.call('wallet.dailyBook', {vendorId:vendor.vendorId,scope:'local',month:'2026-10'});
  assert.ok(Array.isArray(book.days));
  await bridge.close();
  bridge = undefined;
  console.log('Node roundtrip passed: preferences, sources, price revisions/switch/activation, wallet top-up/calibration, persisted receipts and continuous revisions');
} finally {
  if (bridge && bridge.process.exitCode === null) {
    bridge.process.kill();
    await bridge.exited.catch(() => {});
  }
  await fs.rm(directory, { recursive: true, force: true });
}
