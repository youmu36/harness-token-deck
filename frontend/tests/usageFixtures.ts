import type { Metrics, ModelRow } from '../src/features/usage-common/types';
import { mockCollectionStatus } from '../src/infra/bridge/mockCollection';
export function metrics(patch: Partial<Metrics> = {}): Metrics {
  return { recordCount: 2, inputTokens: 100, outputTokens: 20, cacheReadTokens: 60, newInput: 40,
    totalTokens: 120, cacheHitRate: 0.6, cacheCoverageIncomplete: false, picoUsd: '1000000000000',
    incomplete: false, unpriced: false, unknownModelCount: 0, missingUsageCount: 0, pendingValuationCount: 0,
    estimatePico: '1000000000000', correctionPico: '0', unallocatedCorrectionPico: '0', allocationIncomplete: false, ...patch };
}
export function model(modelKey = 'gpt-6', patch: Partial<ModelRow> = {}): ModelRow {
  return { ...metrics(), modelKey, displayName: modelKey || '模型未知', ...patch };
}
export const collectionStatus = () => mockCollectionStatus('2026-10-06T06:33:00.000Z', '2026-10-06T06:32:00.000Z', 1);
