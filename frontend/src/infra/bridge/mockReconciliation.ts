import type { CommandData, RequestMap } from '../../app/ports';
import { canonicalPayload } from '../../shared/payload';

export function reconciliationPreview(query: RequestMap['reconciliation.preview']): CommandData<'reconciliation.preview'> {
  let hash = 2166136261;
  for (const char of canonicalPayload(query)) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return {
    vendorId: query.vendorId, start: query.start, end: query.end, businessTimezone: 'Asia/Shanghai',
    memberDays: [new Date(Date.parse(query.start) + 8 * 3600000).toISOString().slice(0, 10)],
    memberCount: '1', memberDigest: 'a'.repeat(64), batchIds: query.localBatchIds?.length ? query.localBatchIds : ['local-example'],
    originalEstimatePico: '700000000000', currentEstimatePico: '700000000000', currentEffectivePico: '700000000000',
    existingCorrectionPico: '0', pendingCount: '0', incompleteCount: '0', harnesses: ['codex'], modelKeys: ['gpt-6'], groupIds: ['standard'],
    localComplete: true, quiet: true, coverageAligned: !!query.intervalIds?.length, omittedMemberCount: '0',
    auxiliaryIncluded: false, externalUsagePossible: true, legacyOverrides: 0, legacyFlowIds: [], legacyAllocationIncomplete: false, unallocatedCorrectionPico: '0', allocationIncomplete: false, independentCorrectionPico: '0',
    fingerprint: (hash >>> 0).toString(16).padStart(8, '0').repeat(8), snapshot: { usage: 1, wallet: 1, account: 1, sources: 1 }, notices: [],
    remoteTargetPico: query.intervalIds?.length ? '1000000000000' : null, remoteUsedDeltaPico: query.intervalIds?.length ? '1000000000000' : null,
    bookedEstimatePico: '700000000000', reliability: query.intervalIds?.length ? 'reliable_usage' : 'missing',
    remoteIntervals: (query.intervalIds ?? []).map(id => ({ id, start: query.start, end: query.end, revision: 1, method: 'remote_usage', unitVersion: 'native-USD-v1', consumptionPico:'1000000000000', estimatePico:'700000000000', localStart:query.start, localEnd:query.end, sessionCount:1 })),
  };
}

export function reconciliationBatches(query: RequestMap['reconciliation.batches']): CommandData<'reconciliation.batches'> {
  return {
    batches: [{ id: 'local-example', sessionRef: '0123456789abcdef', start: query.start, end: query.end, revision: 1, publishedRevision: 1, state: 'sealed' }],
    remoteIntervals: [{ id: 'remote-example', start: query.start, end: query.end, revision: 1, method: 'remote_usage', unitVersion: 'native-USD-v1', consumptionPico:'1000000000000', estimatePico:'700000000000', localStart:query.start, localEnd:query.end, sessionCount:1 }],
    nextOffset: null, remoteNextOffset: null, generatedAt: new Date().toISOString(),
  };
}
