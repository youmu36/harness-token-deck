import { AppError } from '../../app/errors';
import type { CommandData, RequestMap } from '../../app/ports';
import { canonicalPayload } from '../../shared/payload';
import type { createMockPricing } from './mockPricing';
import { generateMockSummary, parseMockInstant } from './mockStats';

export function createMockMaintenance(pricing: ReturnType<typeof createMockPricing>, usageRevision: () => number, bumpUsage: (harnesses: ('claude' | 'codex' | 'zcode' | 'dsh')[]) => number) {
  const receipts = new Map<string, { fingerprint: string; result: unknown }>();
  const replacements: { harness: string; start: number; end: number; factor: bigint }[] = [];
  const fail = (code: string): never => { throw new AppError({ code, message: code, retryable: false }); };
  function receipt(command: string, request: { operationId: string }) {
    const fingerprint = canonicalPayload([command, request]); const old = receipts.get(request.operationId);
    if (old && old.fingerprint !== fingerprint) fail('OPERATION_ID_REUSED'); return { fingerprint, result: old?.result };
  }
  function cost(harness: string, at: number, pico: bigint) {
    const replacement = [...replacements].reverse().find(r => r.harness === harness && at >= r.start && at < r.end);
    return pico * (replacement?.factor ?? 1n);
  }
  function preview(request: RequestMap['maintenance.previewRecalc']): CommandData<'maintenance.previewRecalc'> {
    const start = parseMockInstant(request.start); const end = parseMockInstant(request.end); const now = Date.now();
    if (end <= start) fail('RANGE_INVALID'); if (end - start > 72 * 3600000) fail('RECALC_RANGE_TOO_LONG');
    if (start < now - 72 * 3600000) fail('RECALC_TOO_OLD'); if (end > now + 60000) fail('RECALC_IN_FUTURE');
    const catalog = pricing.catalog(); const group = catalog.groups.find(g => g.id === request.groupId) ?? fail('GROUP_NOT_FOUND');
    const version = group.versions.find(v => v.id === request.versionId) ?? fail('VERSION_GROUP_MISMATCH');
    const params = { ...request, offsetMinutes: 0, grain: 'day' as const };
    const old = generateMockSummary(params, cost).metrics; const next = generateMockSummary(params, (_h, _at, pico) => pico * BigInt(version.number)).metrics;
    const delta = BigInt(next.picoUsd) - BigInt(old.picoUsd);
    const vendorId = catalog.groups.find(g => g.id === catalog.current[request.harness]?.groupId)?.vendorId;
    const vendor = pricing.wallets.list(true).vendors.find(v => v.id === vendorId);
    const key = canonicalPayload([request, catalog.revision, pricing.walletRevision(), usageRevision(), old]);
    let hash = 2166136261; for (const char of key) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
    return { totalCount: old.recordCount, pendingCount: 0, unavailableCount: 0, evidenceInsufficientCount: 0, targetRevisionId: version.id, targetRevisionSeq: catalog.revision, ruleCounts: { none: 0, base: old.recordCount, peak: 0, tier: 0 }, recordCount: old.recordCount, oldTotal: old.picoUsd, newTotal: next.picoUsd, unpricedCount: 0, incompleteCount: 0, groupName: group.name, versionDisplayName: version.displayName, fingerprint: (hash >>> 0).toString(16).padStart(8, '0').repeat(8), vendors: vendor ? [{ vendorId: vendor.id, name: vendor.name, consumptionDelta: delta.toString(), balanceDelta: (-delta).toString() }] : [] };
  }
  function commit(request: RequestMap['maintenance.commitRecalc']): CommandData<'maintenance.commitRecalc'> {
    const saved = receipt('maintenance.commitRecalc', request); if (saved.result) return structuredClone(saved.result) as CommandData<'maintenance.commitRecalc'>;
    const { fingerprint, operationId: _id, ...params } = request; const result = preview(params);
    if (result.fingerprint !== fingerprint) fail('RECALC_PLAN_CHANGED');
    const version = pricing.catalog().groups.find(g => g.id === request.groupId)!.versions.find(v => v.id === request.versionId)!;
    replacements.push({ harness: request.harness, start: Date.parse(request.start), end: Date.parse(request.end), factor: BigInt(version.number) });
    pricing.wallets.recalc(result.vendors); const revision = bumpUsage([request.harness]);
    const response = { ...result, batchId: crypto.randomUUID(), revision };
    receipts.set(request.operationId, { fingerprint: saved.fingerprint, result: response }); return response;
  }
  return { preview, commit, cost };
}
