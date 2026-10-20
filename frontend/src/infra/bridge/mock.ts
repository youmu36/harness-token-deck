import packageInfo from '../../../../package.json';
import { AppError } from '../../app/errors';
import type { Bridge, CommandData, CommandName, RequestMap } from '../../app/ports';
import type { PreferencesResponse, PreferenceValues, UsageFilter1 } from '../generated/contracts';
import { createEventHub } from '../../shared/events';
import { canonicalPayload } from '../../shared/payload';
import { createValidatedBridge } from './core';
import { generateMockSummary, generateMockMonitor, parseMockInstant } from './mockStats';
import { mockCollectionStatus } from './mockCollection';
import { createMockPricing } from './mockPricing';
import { createMockSettings } from './mockSettings';
import { createMockTasks } from './mockTasks';
import { createMockMaintenance } from './mockMaintenance';
import { createMockMigration, type MockMigrationOptions } from './mockMigration';
import { createMockRemote } from './mockRemote';
import { mockAnalytics } from './mockAnalytics';

const capabilities = ['stats.analytics', 'app.hello', 'app.sync', 'preferences.get', 'preferences.update', 'stats.summary', 'collection.status', 'collection.scanNow', 'stats.cacheMonitor', 'pricing.catalog', 'pricing.unpriced', 'pricing.resolveUnpriced', 'pricing.recalculateUnpriced', 'pricing.previewSwitch', 'pricing.switchCurrent', 'pricing.createGroup', 'pricing.renameGroup', 'pricing.setGroupDisabled', 'pricing.deleteGroup', 'pricing.addVersion', 'pricing.setVersionLabel', 'pricing.setVersionDisabled', 'pricing.deleteVersion', 'pricing.saveVersionPrices', 'pricing.activateVersion', 'models.pool', 'models.fillByMultiplier', 'wallet.list', 'wallet.entries', 'wallet.createVendor', 'wallet.renameVendor', 'wallet.topUp', 'wallet.calibrate', 'wallet.linkGroups', 'wallet.deleteVendor', 'wallet.restoreVendor', 'wallet.setVisibility', 'wallet.correctEntry', 'wallet.dismissNotice'];
const filterDefaults: UsageFilter1 = {
  range: 'today', harness: 'all', start: null, end: null, grain: 'hour', metric: 'tokens',
  monitorMode: 'minute', modelKey: null, startNow: false, endNow: false,
};

export function createMockBridge(options: { migration?: MockMigrationOptions } = {}): Bridge {
  const hub = createEventHub();
  const pricing = createMockPricing(hub.emit);
  const remote = createMockRemote(pricing.wallets);
  pricing.wallets.setRemoteResolver(remote.resolve);
  let preferences: PreferenceValues = {
    theme: 'system', timezone: 'system', showScanCountdown: false, hiddenHarnesses: [], settingsSection: 'display', walletDefaultScope: 'local', collapsed: false,
    accountingTimezone: 'Asia/Shanghai', unpricedNoticeDismissed: '', lastPage: 'codex', lastRoute: '', guideSeen: false, guideDismissed: false, closeToTray: true, startAtLogin: false, filters: {}, historyPrompted: false, migrationHintDismissed: false,
  };
  let revision = 0;
  let usageRevision = 1;
  const settings = createMockSettings(hub.emit, packageInfo.version, () => revision, () => { revision++; hub.emit('preferences.changed', { revision }); });
  const tasks = createMockTasks(hub.emit);
  const maintenance = createMockMaintenance(pricing, () => usageRevision, harnesses => { usageRevision++; hub.emit('usage.changed', { harnesses, revision: usageRevision }); return usageRevision; });
  let nextPeriodicAt = new Date(Date.now() + 30_000).toISOString();
  let lastCompletedAt: string | null = null;
  const scans = new Map<string, Promise<CommandData<'collection.scanNow'>>>();
  const receipts = new Map<string, { fingerprint: string; snapshot: PreferencesResponse }>();
  const revisions = () => ({ preferences: revision, pricing: pricing.revision(), wallet: pricing.walletRevision(), sources: settings.revision(), usage: usageRevision });
  const snapshot = (): PreferencesResponse => structuredClone({ preferences, revision });
  const migration = createMockMigration(tasks, options.migration ?? {}, () => {
    usageRevision++; revision++; preferences.historyPrompted = true;
    hub.emit('usage.changed', { harnesses: ['claude', 'codex', 'zcode', 'dsh'], revision: usageRevision });
    hub.emit('preferences.changed', { revision });
  });

  function update(request: RequestMap['preferences.update']) {
    const fingerprint = canonicalPayload([request.expectedRevision, request.patch]);
    const receipt = receipts.get(request.operationId);
    if (receipt) {
      if (receipt.fingerprint !== fingerprint) throw new AppError({
        code: 'OPERATION_ID_REUSED', message: '操作标识已用于其他操作', retryable: false,
      });
      return structuredClone(receipt.snapshot);
    }
    if (request.expectedRevision !== revision) throw new AppError({
      code: 'PREFERENCES_REVISION_CONFLICT', message: '设置已更新，请刷新后重新确认',
      field: 'expectedRevision', conflictRevision: revision, retryable: false,
    });
    for (const [field, value] of Object.entries(request.patch)) {
      if (value === null) throw new AppError({ code: 'INVALID_REQUEST', message: '偏好字段不能为空', field: `patch.${field}`, retryable: false });
    }
    const filters = { ...preferences.filters };
    for (const [page, value] of Object.entries(request.patch.filters ?? {})) {
      const filter = { ...filterDefaults, ...value };
      if (filter.range === 'custom') {
        if (!filter.start || !filter.end) throw new AppError({ code: 'INVALID_REQUEST', message: '请填写完整的自定义范围', field: `patch.filters.${page}`, retryable: false });
        const span = parseMockInstant(filter.end) - parseMockInstant(filter.start);
        if (span <= 0 || span > 30 * 86_400_000) throw new AppError({ code: 'INVALID_REQUEST', message: '自定义范围须大于零且不超过 30 天', field: `patch.filters.${page}`, retryable: false });
      }
      filters[page] = filter;
    }
    const next = { ...preferences, ...request.patch, filters } as PreferenceValues;
    const changed = canonicalPayload(preferences) !== canonicalPayload(next);
    preferences = structuredClone(next);
    if (changed) revision++;
    const result = snapshot();
    receipts.set(request.operationId, { fingerprint, snapshot: result });
    if (changed) hub.emit('preferences.changed', { revision });
    return structuredClone(result);
  }

  function scan(operationId: string) {
    const existing = scans.get(operationId);
    if (existing) return existing;
    const result = new Promise<CommandData<'collection.scanNow'>>(resolve => {
      setTimeout(() => {
        lastCompletedAt = new Date().toISOString();
        nextPeriodicAt = new Date(Date.parse(lastCompletedAt) + 30_000).toISOString();
        settings.completed(lastCompletedAt); usageRevision++;
        hub.emit('usage.changed', { harnesses: ['claude', 'codex', 'zcode', 'dsh'], revision: usageRevision });
        hub.emit('collection.tick', { status: { claude: 'watching', codex: 'watching', zcode: 'watching', dsh: 'watching' },
          nextPeriodicAt, lastCompletedAt, trigger: 'manual' });
        resolve({ completedAt: lastCompletedAt, nextPeriodicAt });
      }, 150);
    });
    scans.set(operationId, result);
    return result;
  }

  const bridge = createValidatedBridge('preview', async (command: CommandName, payload: object) => {
    try {
      switch (command) {
        case 'app.hello': {
          if ((payload as RequestMap['app.hello']).clientProtocol !== 1) throw new AppError({
            code: 'PROTOCOL_MISMATCH', message: '界面与应用版本不兼容', field: 'clientProtocol', retryable: false,
          });
          // The transport mode is on Bridge; the generated hello contract requires desktop.
          return { ok: true, data: { protocol: 1, appVersion: packageInfo.version, capabilities: [...capabilities, 'models.add', 'models.remove', 'models.restore', 'models.saveBaseRates', 'models.scan', 'wallet.scanProviders', 'wallet.createScannedVendors', 'tasks.get', 'tasks.cancel', 'sources.list', 'sources.validatePath', 'sources.setPath', 'sources.autoDetect', 'collection.setPaused', 'maintenance.status', 'maintenance.previewRetention', 'maintenance.setRetention', 'maintenance.backup', 'maintenance.vacuum', 'maintenance.previewRecalc', 'maintenance.commitRecalc', 'app.pickPath', 'app.openPath', 'migration.detect', 'migration.run', 'migration.activate', 'remote.get', 'remote.test', 'remote.save', 'remote.refresh', 'remote.delete', 'reconciliation.status'], mode: 'desktop', dataDir: settings.status().dataDir, revisions: revisions() } };
        }
        case 'app.sync': return { ok: true, data: { revisions: revisions(), pendingEvents: [] } };
        case 'remote.get': return { ok: true, data: remote.get((payload as RequestMap['remote.get']).vendorId) };
        case 'remote.test': return { ok: true, data: remote.test(payload as RequestMap['remote.test']) };
        case 'remote.save': case 'remote.refresh': case 'remote.delete': return { ok: true, data: remote.change(command, payload as RequestMap[typeof command]) };
        case 'reconciliation.status': return { ok: true, data: { notices: [] } };
        case 'stats.analytics': return { ok: true, data: mockAnalytics(payload as RequestMap['stats.analytics']) };
        case 'preferences.get': return { ok: true, data: snapshot() };
        case 'preferences.update': return { ok: true, data: update(payload as RequestMap['preferences.update']) };
        case 'stats.longTerm': return {ok:true,data:{totals:{records:'0',input:'0',cache_read:'0',output:'0',pico:'0'},activeMinutes:'0',coveredDays:0,businessTimezone:'Asia/Shanghai',includesWholeBoundaryDays:false,missingCapabilities:[],basis:'本地历史估算'}};
        case 'stats.auxiliaryCosts': return { ok: true, data: {costPico:'1000000000',models:[{modelId:'gpt-6-mini',purpose:'生成会话标题',count:1,costPico:'1000000000',tokens:null}],revision:1} };
        case 'pricing.unpricedNotice': return {ok:true,data:{count:0,harnesses:[],signature:'0:0'}};
        case 'wallet.dailyBook': return {ok:true,data:{days:[],R:'0',C:'0',A:'0',pending:false,notice:'仅含已结束业务日',revision:0,providerTodayPico:null,providerTotalPico:null,providerObservedAt:null}};
        case 'wallet.setLocalTotals': return {ok:false,error:{code:'PREVIEW_ONLY',message:'金额修改请在桌面应用中使用真实账本验证',retryable:false}};
        case 'wallet.accountEntries': return {ok:true,data:pricing.wallets.accountPage(payload as RequestMap['wallet.accountEntries'])};
        case 'wallet.reclassify': case 'wallet.correctConsumption': case 'wallet.correctEntry': case 'wallet.dismissNotice': return {ok:false,error:{code:'PREVIEW_ONLY',message:'此操作请在桌面应用中使用真实账本验证',retryable:false}};
        case 'stats.summary': return { ok: true, data: { ...generateMockSummary(payload as RequestMap['stats.summary'], maintenance.cost), revision: usageRevision } };
        case 'stats.cacheMonitor': return { ok: true, data: { ...generateMockMonitor(payload as RequestMap['stats.cacheMonitor']), revision: usageRevision } };
        case 'pricing.unpriced': return { ok: true, data: { limited:false, items: [], archived: [], ignoredCount: 0, usageRevision, recordCount: 0, readyCount: 0, pricingRevision: pricing.revision(), start: new Date(Date.now() - 30 * 86400000).toISOString(), end: new Date().toISOString() } };
        case 'pricing.resolveUnpriced': throw new AppError({ code: 'UNPRICED_CHANGED', message: '示例环境没有待处理请求', field: null, retryable: false });
        case 'pricing.recalculateUnpriced': return { ok: true, data: { complete:true,errorCode:null,recalculated: 0, remaining: 0, costDeltaPico: '0', revision: usageRevision } };
        case 'pricing.catalog': return { ok: true, data: pricing.catalog() };
        case 'pricing.previewSwitch': return { ok: true, data: pricing.preview(payload as RequestMap['pricing.previewSwitch']) };
        case 'pricing.switchCurrent': return { ok: true, data: pricing.switchCurrent(payload as RequestMap['pricing.switchCurrent']) };
        case 'pricing.createGroup': case 'pricing.renameGroup': case 'pricing.setGroupDisabled': case 'pricing.deleteGroup': case 'pricing.addVersion': case 'pricing.setVersionLabel': case 'pricing.saveVersionPrices': case 'pricing.activateVersion': return { ok: true, data: pricing.edit(command, payload as RequestMap[typeof command]) };
        case 'models.pool': return { ok: true, data: pricing.pool() };
        case 'models.add': case 'models.remove': case 'models.restore': case 'models.saveBaseRates': return { ok: true, data: pricing.editModels(command, payload as RequestMap[typeof command]) };
        case 'models.scan': return { ok: true, data: tasks.start(command, payload as RequestMap['models.scan'], () => { const pool = pricing.pool(); const candidates = [...new Set([...pool.models.map(m => m.modelId), ...pool.excluded, 'new-source-model'])]; return { start: new Date(Date.now() - 3 * 86400000).toISOString(), end: new Date().toISOString(), revision: pool.revision, diagnostics: [], models: candidates.map(modelId => ({ modelId, inPool: pool.models.some(m => m.modelId.toLowerCase() === modelId.toLowerCase()), excluded: pool.excluded.includes(modelId.toLowerCase()) })) }; }) };
        case 'tasks.get': return { ok: true, data: tasks.get((payload as RequestMap['tasks.get']).taskId) };
        case 'tasks.cancel': return { ok: true, data: tasks.cancel(payload as RequestMap['tasks.cancel']) };
        case 'migration.detect': return { ok: true, data: migration.detect((payload as RequestMap['migration.detect']).sourcePath) };
        case 'migration.run': return { ok: true, data: migration.run(payload as RequestMap['migration.run']) };
        case 'migration.activate': return { ok: true, data: migration.activate(payload as RequestMap['migration.activate']) };
        case 'maintenance.previewRecalc': return { ok: true, data: maintenance.preview(payload as RequestMap['maintenance.previewRecalc']) };
        case 'maintenance.commitRecalc': return { ok: true, data: maintenance.commit(payload as RequestMap['maintenance.commitRecalc']) };
        case 'sources.list': return { ok: true, data: settings.list() };
        case 'sources.validatePath': return { ok: true, data: settings.validate(payload as RequestMap['sources.validatePath']) };
        case 'sources.setPath': case 'sources.autoDetect': case 'collection.setPaused': return { ok: true, data: settings.change(command, payload as RequestMap[typeof command]) };
        case 'maintenance.status': return { ok: true, data: settings.status() };
        case 'maintenance.previewRetention': return { ok: true, data: settings.preview(payload as RequestMap['maintenance.previewRetention']) };
        case 'maintenance.setRetention': return { ok: true, data: settings.setRetention(payload as RequestMap['maintenance.setRetention']) };
        case 'maintenance.backup': case 'maintenance.vacuum': return { ok: true, data: tasks.start(command, payload as RequestMap[typeof command], () => settings.fileResult(command === 'maintenance.backup' ? 'backup' : 'vacuum')) };
        case 'app.pickPath': case 'app.openPath': throw new AppError({ code: 'DESKTOP_REQUIRED', message: '原生文件选择与打开目录请在桌面应用中使用', retryable: false });
        case 'models.fillByMultiplier': return { ok: true, data: pricing.fill(payload as RequestMap['models.fillByMultiplier']) };
        case 'wallet.scanProviders': return { ok: true, data: tasks.start(command, payload as RequestMap['wallet.scanProviders'], () => ({ kind: 'providers', start: new Date(Date.now() - 3 * 86400000).toISOString(), end: new Date().toISOString(), providers: pricing.wallets.providerCandidates(), diagnostics: [], missingProviderCount: 2 })) };
        case 'wallet.createScannedVendors': {
          const request = payload as RequestMap['wallet.createScannedVendors']; const task = tasks.get(request.taskId);
          if (task.status !== 'completed' || task.result?.kind !== 'providers') throw new AppError({ code: 'PROVIDER_SCAN_INVALID', message: '请先完成扫描', retryable: false });
          return { ok: true, data: pricing.wallets.createScanned(request, task.result.providers as ReturnType<typeof pricing.wallets.providerCandidates>) };
        }
        case 'wallet.list': return { ok: true, data: pricing.wallets.list((payload as RequestMap['wallet.list']).includeArchived) };
        case 'wallet.entries': return { ok: true, data: pricing.wallets.page(payload as RequestMap['wallet.entries']) };
        case 'wallet.createVendor': case 'wallet.renameVendor': case 'wallet.topUp': case 'wallet.calibrate': case 'wallet.linkGroups': case 'wallet.deleteVendor': case 'wallet.restoreVendor': case 'wallet.setVisibility': return { ok: true, data: pricing.wallets.change(command, payload as RequestMap[typeof command]) };
        case 'collection.status': { const list = settings.list(); return { ok: true, data: { ...mockCollectionStatus(nextPeriodicAt, lastCompletedAt, list.revision), ...list, state: list.paused ? 'paused' : 'idle' } }; }
        case 'collection.scanNow': { if (settings.list().paused) throw new AppError({ code: 'COLLECTION_PAUSED', message: '采集已暂停', retryable: false }); return { ok: true, data: await scan((payload as RequestMap['collection.scanNow']).operationId) }; }
        default: throw new AppError({ code: 'UNKNOWN_COMMAND', message: '浏览器预览暂未实现此操作', retryable: false });
      }
    } catch (failure) {
      if (failure instanceof AppError) return { ok: false, error: failure.info };
      throw failure;
    }
  }, hub);
  return { ...bridge, dispose: () => { tasks.dispose(); bridge.dispose(); } };
}

