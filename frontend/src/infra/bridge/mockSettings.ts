import type { CommandData, EventMap, EventName, RequestMap } from '../../app/ports';
import { AppError } from '../../app/errors';
import { canonicalPayload } from '../../shared/payload';
import { mockCollectionStatus } from './mockCollection';
export function createMockSettings(emit: <K extends EventName>(name: K, payload: EventMap[K]) => void, version: string, preferencesRevision: () => number, bumpPreferences: () => void) {
  const paths = { claude: 'C:\\Preview\\claude\\projects', codex: 'C:\\Preview\\codex\\sessions', zcode: 'C:\\Preview\\zcode\\data.db', dsh: 'C:\\Preview\\dsh\\sessions' };
  const sources = mockCollectionStatus('', null, 0).sources.map(source => ({ ...source, path: paths[source.harness] }));
  let revision = 0; let paused = false; let retention: RequestMap['maintenance.setRetention']['retention'] = 360;
  let lastRetentionAt: string | null = null; let lastVacuumAt: string | null = null;
  const receipts = new Map<string, { fingerprint: string; result: unknown }>();
  const fail = (code: string): never => { throw new AppError({ code, message: code, retryable: false }); };
  function validate(request: RequestMap['sources.validatePath']): CommandData<'sources.validatePath'> {
    if (!/^(?:[A-Za-z]:[\\/]|\\\\)/.test(request.path)) return fail('PATH_NOT_ABSOLUTE');
    const file = /\.(?:db|sqlite3?)$/i.test(request.path); const available = !/missing|不存在/.test(request.path) && (request.harness === 'zcode' ? file : !file);
    return { ...request, available, diagnostics: available ? [] : [request.harness === 'zcode' ? 'source_database_unavailable' : 'source_directory_unavailable'] };
  }
  const list = (): CommandData<'sources.list'> => structuredClone({ sources, revision, paused });
  type Edit = 'sources.setPath' | 'sources.autoDetect' | 'collection.setPaused';
  function change(command: Edit, request: RequestMap[Edit]): CommandData<'sources.list'> | CommandData<'collection.setPaused'> {
    const fingerprint = canonicalPayload([command, request]); const old = receipts.get(request.operationId);
    if (old) { if (old.fingerprint !== fingerprint) return fail('OPERATION_ID_REUSED'); return structuredClone(old.result) as ReturnType<typeof change>; }
    if ('expectedRevision' in request && request.expectedRevision !== revision) return fail('SOURCES_REVISION_CONFLICT');
    const before = canonicalPayload([sources, paused]);
    if (command === 'collection.setPaused') paused = (request as RequestMap['collection.setPaused']).paused;
    else if (command === 'sources.setPath') {
      const value = request as RequestMap['sources.setPath']; const valid = validate({ harness: value.harness, path: value.path });
      const source = sources.find(s => s.harness === value.harness)!;
      if (source.path !== value.path) Object.assign(source, { path: value.path, status: valid.available ? 'pending' : 'unavailable', diagnostics: valid.diagnostics, unmeteredRequests: null });
    } else for (const source of sources) if (source.path !== paths[source.harness]) Object.assign(source, { path: paths[source.harness], status: 'pending', diagnostics: [], unmeteredRequests: null });
    if (before !== canonicalPayload([sources, paused])) { revision++; emit('sources.changed', { revision }); }
    const result = command === 'collection.setPaused' ? { paused, revision } : list(); receipts.set(request.operationId, { fingerprint, result }); return structuredClone(result);
  }
  function preview(value: RequestMap['maintenance.previewRetention']): CommandData<'maintenance.previewRetention'> {
    const expired = value.retention !== 'forever' && value.retention < 360;
    return { retention: value.retention, cutoff: value.retention === 'forever' ? null : new Date(Date.now() - value.retention * 86400000).toISOString(), ledgerCount: expired ? 12 : 0, inboxCount: expired ? 3 : 0, identityCount: 100, earliest: expired ? '2025-10-01T00:00:00.000Z' : null, latest: expired ? '2025-10-05T00:00:00.000Z' : null };
  }
  function setRetention(request: RequestMap['maintenance.setRetention']): CommandData<'maintenance.setRetention'> {
    const fingerprint = canonicalPayload(['maintenance.setRetention', request]); const old = receipts.get(request.operationId);
    if (old) { if (old.fingerprint !== fingerprint) return fail('OPERATION_ID_REUSED'); return structuredClone(old.result) as CommandData<'maintenance.setRetention'>; }
    if (request.expectedRevision !== preferencesRevision()) return fail('PREFERENCES_REVISION_CONFLICT');
    const counts = preview({ retention: request.retention }); if (retention !== request.retention) { retention = request.retention; bumpPreferences(); }
    lastRetentionAt = new Date().toISOString(); const result = { retention, revision: preferencesRevision(), cutoff: counts.cutoff, deletedLedger: counts.ledgerCount, expiredInbox: counts.inboxCount, complete: true, errorCode: null };
    receipts.set(request.operationId, { fingerprint, result }); return structuredClone(result);
  }
  const dataDir = 'C:\\Preview\\HarnessTokenDeck';
  return { list, validate, change, revision: () => revision, preview, setRetention,
    status: (): CommandData<'maintenance.status'> => ({ paused, retention, lastRetentionAt, lastVacuumAt, lastLogRotationAt: null, pendingExpired: 15, logCount: 24, databaseBytes: 1048576, storage: null, appVersion: version, dataDir }),
    fileResult: (kind: 'backup' | 'vacuum') => { const completedAt = new Date().toISOString(); if (kind === 'vacuum') lastVacuumAt = completedAt; return { completedAt, path: kind === 'backup' ? `${dataDir}\\backups\\example.sqlite3` : null }; },
    completed: (at: string) => { for (const source of sources) { source.lastScanAt = at; if (validate({ harness: source.harness, path: source.path }).available) source.status = 'watching'; } revision++; emit('sources.changed', { revision }); },
  };
}
