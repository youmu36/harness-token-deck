import type { CommandData } from '../../app/ports';

/** Isolated synthetic sources; preview never reads local collection paths. */
export function mockCollectionStatus(nextPeriodicAt: string, completedAt: string | null, revision: number): CommandData<'collection.status'> {
  const startedAt = new Date(); startedAt.setHours(0, 0, 0, 0);
  return {
    sources: (['codex', 'zcode', 'dsh', 'claude'] as const).map(harness => ({
      harness, path: `示例来源/${harness}`, startedAt: startedAt.toISOString(), lastScanAt: completedAt,
      status: 'watching', diagnostics: [], unmeteredRequests: null, scanEnabled: true,
      identityCount: 24, processedCount: 24, inboxCount: 0, ledgerCount: 24,
    })),
    revision, paused: false, state: 'idle', nextPeriodicAt, lastCompletedAt: completedAt, trigger: completedAt ? 'manual' : null, lastError: null,
  };
}
