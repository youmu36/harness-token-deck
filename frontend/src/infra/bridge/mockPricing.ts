import type { CommandData, RequestMap } from '../../app/ports';
import type { EventMap, EventName } from '../../app/ports';
import { AppError } from '../../app/errors';
import { canonicalPayload } from '../../shared/payload';
import type { ModelPrice1, Rule } from '../generated/contracts';
import { createMockWallet } from './mockWallet';

export function mockPricingCatalog(): CommandData<'pricing.catalog'> {
  const createdAt = '2026-10-01T00:00:00.000Z';
  const version = (id: string, number: number, label: string, current: boolean) => ({
    id, number, label, displayName: label || `v${number}`, current, disabled: false, createdAt,
    revisionId: `${id}-revision`, revisionCount: 1, hints: [], models: [{ modelId: 'gpt-6', input: '1.5', cache: '0.15', output: '6', cacheWrite: null, billingRule: null }],
  });
  return { revision: 1, current: { claude: { groupId: 'standard', versionId: 'standard-v1' }, codex: { groupId: 'standard', versionId: 'standard-v1' }, zcode: { groupId: 'standard', versionId: 'standard-v1' }, dsh: { groupId: 'alternate', versionId: 'alternate-v2' } }, groups: [
    { id: 'standard', historical: false, nextVersionNumber: 3, name: '标准价格', disabled: false, createdAt, harnesses: ['claude', 'codex', 'zcode'], canDelete: true, deleteReason: null, vendorId: 'vendor-standard', versions: [version('standard-v1', 1, '', true), version('standard-v2', 2, '秋季价格', false)] },
    { id: 'alternate', historical: false, nextVersionNumber: 3, name: '备用价格', disabled: false, createdAt, harnesses: ['dsh'], canDelete: true, deleteReason: null, vendorId: 'vendor-alternate', versions: [version('alternate-v1', 1, '', false), version('alternate-v2', 2, '当前方案', true)] },
    { id: 'unlinked', historical: false, nextVersionNumber: 2, name: '未关联价格', disabled: false, createdAt, harnesses: [], canDelete: true, deleteReason: null, vendorId: null, versions: [version('unlinked-v1', 1, '', true)] },
    { id: 'disabled', historical: false, nextVersionNumber: 2, name: '已停用价格', disabled: true, createdAt, harnesses: [], canDelete: true, deleteReason: null, vendorId: null, versions: [version('disabled-v1', 1, '', true)] },
  ] };
}

export function mockWallet(): CommandData<'wallet.list'> {
  const base = { createdAt: '2026-10-01T00:00:00.000Z', status: 'active' as const, url: null, pastTopUpPico: '0', pastConsumptionPico: '0', R: '20000000000000', C: '7661000000000', A: '0', historyCoverageStart: null, unpricedCount: 0, incompleteCount: 0, account: null, localWallet: null, remoteWallet: null, walletRevision: 1, walletVisibility: 'both' as const };
  return { revision: 1, vendors: [
    { ...base, id: 'vendor-standard', name: '示例供应商', B: '12339000000000', linkedGroups: ['standard'], lastCalibratedAt: '2026-10-05T05:34:00.000Z', coverageIncomplete: false },
    { ...base, id: 'vendor-alternate', name: '备用供应商', R: '0', C: '1239000000000', B: '-1239000000000', linkedGroups: ['alternate'], lastCalibratedAt: null, coverageIncomplete: true, incompleteCount: 1 },
  ] };
}

export function createMockPricing(emit: <K extends EventName>(name: K, payload: EventMap[K]) => void) {
  const catalog = mockPricingCatalog();
  const wallet = mockWallet();
  const receipts = new Map<string, { fingerprint: string; result: CommandData<'pricing.switchCurrent'> }>();
  const wallets = createMockWallet(wallet, catalog, emit, id => receipts.has(id));
  const pool: CommandData<'models.pool'> = { revision: 1, excluded: [], models: [
    { modelId: 'gpt-6', rates: { input: '1.5', cache: '0.15', output: '6', cacheWrite: null } },
    { modelId: 'claude-sonnet-4.5', rates: { input: '3', cache: '0.3', output: '15', cacheWrite: null } },
    { modelId: 'deepseek-v3.2', rates: { input: '0.28', cache: '0.028', output: '0.42', cacheWrite: null } },
  ] };
  const fail = (code: string) => { throw new AppError({ code, message: code, retryable: false }); };
  function rate(text: string | null | undefined): string | null {
    if (text == null || text === '') return null;
    if (!/^[0-9]{1,9}(?:\.[0-9]{1,6})?$/.test(text)) return fail('RATE_INVALID');
    return text.replace(/^0+(?=\d)/, '').replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
  }
  function fill(request: RequestMap['models.fillByMultiplier']): CommandData<'models.fillByMultiplier'> {
    if (!/^[0-9]{1,9}(?:\.[0-9]{1,3})?$/.test(request.multiplier)) return fail('MULTIPLIER_INVALID');
    const integer = (text: string, digits: number) => { const [whole, fraction = ''] = text.split('.'); return BigInt(whole!) * 10n ** BigInt(digits) + BigInt(fraction.padEnd(digits, '0')); };
    const scale = integer(request.multiplier, 3);
    const result = (text: string | null | undefined) => {
      const normalized = rate(text); if (normalized === null) return null;
      const value = (integer(normalized, 6) * scale + 500n) / 1000n;
      if (value > 999_999_999_999_999n) return fail('RATE_OVERFLOW');
      return rate(`${value / 1_000_000n}.${String(value % 1_000_000n).padStart(6, '0')}`);
    };
    return { input: result(request.rates.input), cache: result(request.rates.cache), output: result(request.rates.output), cacheWrite: result(request.rates.cacheWrite) };
  }
  type Edit = 'pricing.createGroup' | 'pricing.renameGroup' | 'pricing.setGroupDisabled' | 'pricing.deleteGroup' | 'pricing.addVersion' | 'pricing.setVersionLabel' | 'pricing.saveVersionPrices' | 'pricing.activateVersion' | 'pricing.setVersionDisabled' | 'pricing.deleteVersion';
  function edit(command: Edit, request: RequestMap[Edit]): CommandData<'pricing.switchCurrent'> {
    if (wallets.operationUsed(request.operationId)) return fail('OPERATION_ID_REUSED');
    const fingerprint = canonicalPayload([command, request]); const prior = receipts.get(request.operationId);
    if (prior) { if (prior.fingerprint !== fingerprint) return fail('OPERATION_ID_REUSED'); return structuredClone(prior.result); }
    if (request.expectedRevision !== catalog.revision) throw new AppError({ code: 'PRICING_REVISION_CONFLICT', message: '价格目录已变化', conflictRevision: catalog.revision, retryable: false });
    const next = structuredClone(catalog); let groupId: string | null = null; let versionId: string | null = null; let changed = false; const affectedHarnesses: CommandData<'pricing.switchCurrent'>['affectedHarnesses'] = [];
    const groupById = (id: string) => next.groups.find(g => g.id === id) ?? fail('GROUP_NOT_FOUND');
    const versionById = (id: string) => { const group = next.groups.find(g => g.versions.some(v => v.id === id)); if (!group) return fail('VERSION_NOT_FOUND'); return { group, version: group.versions.find(v => v.id === id)! }; };
    const name = (text: string, except?: string) => { const value = text.trim(); if (!value || value.length > 60) return fail('PRICING_INVALID'); if (next.groups.some(g => g.name === value && g.id !== except)) return fail('GROUP_DUPLICATE'); return value; };
    const newVersion = (number: number, label: string, models: ModelPrice1[] = []) => { const id = crypto.randomUUID(); return { id, number, label, displayName: label || `v${number}`, createdAt: new Date().toISOString(), current: false, disabled: false, revisionId: crypto.randomUUID(), revisionCount: 1, models, hints: [] }; };
    if (command === 'pricing.createGroup') {
      const r = request as RequestMap['pricing.createGroup']; if (next.groups.length >= 100) return fail('GROUP_LIMIT');
      const models = r.copyFromGroupId ? groupById(r.copyFromGroupId).versions.find(v => v.current)!.models : [];
      const version = newVersion(1, '', models); version.current = true; groupId = crypto.randomUUID(); versionId = version.id;
      next.groups.push({ id: groupId, historical: false, nextVersionNumber: 2, name: name(r.name), disabled: false, createdAt: new Date().toISOString(), harnesses: [], canDelete: true, deleteReason: null, vendorId: null, versions: [version] }); changed = true;
    } else if (command === 'pricing.renameGroup') {
      const r = request as RequestMap['pricing.renameGroup']; const group = groupById(r.groupId); groupId = group.id; const value = name(r.name, group.id); changed = group.name !== value; group.name = value;
    } else if (command === 'pricing.setGroupDisabled') {
      const r = request as RequestMap['pricing.setGroupDisabled']; const group = groupById(r.groupId); groupId = group.id; if (r.disabled && group.harnesses.length) return fail('GROUP_IN_USE'); changed = group.disabled !== r.disabled; group.disabled = r.disabled;
    } else if (command === 'pricing.deleteGroup') {
      const r = request as RequestMap['pricing.deleteGroup']; const group = groupById(r.groupId); if (!group.canDelete) return fail('GROUP_REFERENCED'); groupId = group.id; next.groups = next.groups.filter(g => g.id !== group.id); for (const h of ['claude', 'codex', 'zcode', 'dsh'] as const) if (next.current[h]?.groupId === group.id) next.current[h] = { groupId: null, versionId: null }; changed = true;
    } else if (command === 'pricing.addVersion') {
      const r = request as RequestMap['pricing.addVersion']; const group = groupById(r.groupId); if (group.disabled) return fail('GROUP_DISABLED'); if (group.versions.some(v => v.number === r.number)) return fail('VERSION_DUPLICATE');
      const version = newVersion(r.number, r.label ?? '', r.copyFromVersionId ? versionById(r.copyFromVersionId).version.models : []); group.versions.push(version); group.nextVersionNumber = Math.max(group.nextVersionNumber, r.number + 1); groupId = group.id; versionId = version.id; changed = true;
    } else if (command === 'pricing.setVersionDisabled' || command === 'pricing.deleteVersion') {
      const r = request as RequestMap['pricing.setVersionDisabled']; const { group, version } = versionById(r.versionId);
      if (version.current) return fail('VERSION_IN_USE'); groupId = group.id; versionId = version.id; changed = true;
      if (command === 'pricing.deleteVersion') group.versions = group.versions.filter(v => v.id !== version.id); else version.disabled = r.disabled;
    } else if (command === 'pricing.setVersionLabel') {
      const r = request as RequestMap['pricing.setVersionLabel']; const { version } = versionById(r.versionId); versionId = version.id; changed = version.label !== r.label.trim(); version.label = r.label.trim(); version.displayName = version.label || `v${version.number}`;
    } else if (command === 'pricing.activateVersion') {
      const r = request as RequestMap['pricing.activateVersion']; const group = groupById(r.groupId); const found = group.versions.find(v => v.id === r.versionId); if (!found) return fail('VERSION_GROUP_MISMATCH'); if (group.disabled) return fail('GROUP_DISABLED'); groupId = group.id; versionId = found.id; changed = !found.current;
      if (changed) { affectedHarnesses.push(...group.harnesses); group.versions.forEach(v => { v.current = v.id === r.versionId; }); for (const selection of Object.values(next.current)) if (selection.groupId === group.id) selection.versionId = found.id; }
    } else {
      const r = request as RequestMap['pricing.saveVersionPrices']; const { group, version } = versionById(r.versionId); if (group.disabled) return fail('GROUP_DISABLED'); versionId = version.id;
      const normalizeRule = (rule?: Rule | null) => rule ? { ...rule, rates: { input: rate(rule.rates.input), cache: rate(rule.rates.cache), output: rate(rule.rates.output), cacheWrite: rate(rule.rates.cacheWrite) }, weekdays: [...new Set(rule.weekdays)].sort((a,b) => a-b) as Rule['weekdays'], hours: [...new Set(rule.hours)].sort((a,b) => a-b) as Rule['hours'] } : null;
      const models: ModelPrice1[] = r.models.map(m => ({ modelId: m.modelId.trim(), input: rate(m.input), cache: rate(m.cache), output: rate(m.output), cacheWrite: rate(m.cacheWrite), billingRule: normalizeRule(m.billingRule) }));
      if (models.some(m => !m.modelId) || new Set(models.map(m => m.modelId.toLowerCase())).size !== models.length) return fail('MODEL_DUPLICATE');
      const sorted = (rows: ModelPrice1[]) => [...rows].sort((a,b) => a.modelId.toLowerCase().localeCompare(b.modelId.toLowerCase()));
      const pricesChanged = canonicalPayload(sorted(models)) !== canonicalPayload(sorted(version.models));
      const hints = new Map(version.hints.map(h => [h.modelId.toLowerCase(), h])); for (const h of r.hints ?? []) hints.set(h.modelId.toLowerCase(), h);
      changed = pricesChanged || canonicalPayload([...hints.values()]) !== canonicalPayload(version.hints);
      if (pricesChanged) { version.models = models; version.revisionCount++; version.revisionId = crypto.randomUUID(); } version.hints = [...hints.values()];
    }
    if (changed) next.revision++;
    
    Object.assign(catalog, next); const result = { changed, revision: catalog.revision, groupId, versionId, affectedHarnesses };
    receipts.set(request.operationId, { fingerprint, result: structuredClone(result) }); if (changed) emit('pricing.changed', { revision: catalog.revision }); return structuredClone(result);
  }
  function preview(request: RequestMap['pricing.previewSwitch']): CommandData<'pricing.previewSwitch'> {
    const group = catalog.groups.find(group => group.id === request.groupId);
    if (!group) throw new AppError({ code: 'GROUP_NOT_FOUND', message: '价格组不存在', retryable: false });
    if (group.disabled) throw new AppError({ code: 'GROUP_DISABLED', message: '价格组已停用', retryable: false });
    const version = group.versions.find(version => version.id === request.versionId);
    if (!version) throw new AppError({ code: 'VERSION_GROUP_MISMATCH', message: '版本不属于此组', retryable: false });
    const changed = catalog.current[request.harness]?.groupId !== group.id || !version.current;
    const affectedHarnesses = (['claude', 'codex', 'zcode', 'dsh'] as const).filter(harness =>
      (!version.current && catalog.current[harness]?.groupId === group.id) || (changed && harness === request.harness));
    return { changed, affectedHarnesses, revision: catalog.revision, groupId: null, versionId: null };
  }
  type ModelEdit = 'models.add' | 'models.restore' | 'models.remove' | 'models.saveBaseRates';
  function editModels(command: ModelEdit, request: RequestMap[ModelEdit]) {
    if (wallets.operationUsed(request.operationId)) return fail('OPERATION_ID_REUSED');
    const fingerprint = canonicalPayload([command, request]); const previous = receipts.get(request.operationId);
    if (previous) { if (previous.fingerprint !== fingerprint) return fail('OPERATION_ID_REUSED'); return structuredClone(previous.result); }
    if (request.expectedRevision !== catalog.revision) throw new AppError({ code: 'PRICING_REVISION_CONFLICT', message: '价格目录已变化', conflictRevision: catalog.revision, retryable: false });
    const next = structuredClone(pool); const key = (name: string) => name.trim().toLowerCase();
    if (command === 'models.remove') {
      const name = (request as RequestMap['models.remove']).modelId; next.models = next.models.filter(m => key(m.modelId) !== key(name));
      if (!next.excluded.includes(key(name))) next.excluded.push(key(name));
    } else if (command === 'models.saveBaseRates') {
      for (const base of (request as RequestMap['models.saveBaseRates']).models) {
        const row = next.models.find(m => key(m.modelId) === key(base.modelId)); if (!row) return fail('MODEL_NOT_FOUND');
        row.rates = { input: rate(base.rates.input), cache: rate(base.rates.cache), output: rate(base.rates.output), cacheWrite: rate(base.rates.cacheWrite) };
      }
    } else {
      for (const name of (request as RequestMap['models.add']).modelIds) {
        const modelId = name.trim(); if (!modelId || modelId.length > 160) return fail('MODEL_INVALID');
        if (!next.models.some(m => key(m.modelId) === key(modelId))) { if (next.models.length >= 5000) return fail('MODEL_POOL_LIMIT'); next.models.push({ modelId, rates: { input: null, cache: null, output: null, cacheWrite: null } }); }
        next.excluded = next.excluded.filter(m => m !== key(modelId));
      }
    }
    const changed = canonicalPayload(next) !== canonicalPayload(pool);
    Object.assign(pool, next); if (changed) catalog.revision++;
    const result = { changed, revision: catalog.revision, groupId: null, versionId: null, affectedHarnesses: [] };
    receipts.set(request.operationId, { fingerprint, result }); if (changed) emit('pricing.changed', { revision: catalog.revision }); return structuredClone(result);
  }
  function switchCurrent(request: RequestMap['pricing.switchCurrent']) {
    if (wallets.operationUsed(request.operationId)) return fail('OPERATION_ID_REUSED');
    const { operationId, ...payload } = request;
    const fingerprint = canonicalPayload(payload);
    const receipt = receipts.get(operationId);
    if (receipt) {
      if (receipt.fingerprint !== fingerprint) throw new AppError({ code: 'OPERATION_ID_REUSED', message: '操作标识已使用', retryable: false });
      return structuredClone(receipt.result);
    }
    if (request.expectedRevision !== catalog.revision) throw new AppError({ code: 'PRICING_REVISION_CONFLICT', message: '价格目录已更新', conflictRevision: catalog.revision, retryable: false });
    const result = preview(request);
    if (result.changed) {
      catalog.current[request.harness] = { groupId: request.groupId, versionId: request.versionId };
      for (const group of catalog.groups) {
        group.harnesses = (['claude', 'codex', 'zcode', 'dsh'] as const).filter(harness => catalog.current[harness]?.groupId === group.id);
        
        if (group.id === request.groupId) for (const version of group.versions) version.current = version.id === request.versionId;
      }
      for (const selection of Object.values(catalog.current)) {
        if (selection.groupId === request.groupId) selection.versionId = request.versionId;
      }
      catalog.revision++; result.revision = catalog.revision;
    }
    result.groupId = request.groupId; result.versionId = request.versionId;
    receipts.set(operationId, { fingerprint, result: structuredClone(result) });
    if (result.changed) emit('pricing.changed', { revision: catalog.revision });
    return result;
  }
  return { catalog: () => structuredClone(catalog), wallets, wallet: () => structuredClone(wallet), preview, switchCurrent, edit, fill, pool: () => structuredClone({ ...pool, revision: catalog.revision }),
    editModels, revision: () => catalog.revision, walletRevision: () => wallet.revision };
}
