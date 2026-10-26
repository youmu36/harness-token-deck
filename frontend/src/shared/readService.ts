import type { Bridge, CommandData, RequestMap } from '../app/ports';
import { AppError } from '../app/errors';
import { canonicalPayload } from './payload';

type ReadName = 'stats.explore' | 'stats.summary' | 'stats.outputReferences' | 'stats.longTerm' | 'stats.exploreOptions' | 'stats.evidence';
type Entry = { promise: Promise<unknown>; readers: Set<symbol>; done: boolean; cancel: () => void };
const active = new WeakMap<Bridge, Map<string, Entry>>();
let sequence = 0;
const cancelled = () => new AppError({ code: 'READ_CANCELLED', message: '读取已取消', retryable: true });

/** Share one transport read; only the last departing reader cancels database work. */
export function readShared<K extends ReadName>(bridge: Bridge, name: K, parameters: RequestMap[K], signal: AbortSignal): Promise<CommandData<K>> {
  if (signal.aborted) return Promise.reject(cancelled());
  const normalized = { ...parameters } as Record<string, unknown>;
  delete normalized.queryId;
  for (const key of ['harnesses', 'sessionRefs', 'activityBatchIds', 'vendorIds', 'groupIds', 'versionIds', 'modelKeys', 'sections', 'compareIds', 'qualityReasons']) {
    if (Array.isArray(normalized[key])) normalized[key] = [...new Set(normalized[key])].sort();
  }
  const key = canonicalPayload([name, normalized]);
  let queries = active.get(bridge);
  if (!queries) { queries = new Map(); active.set(bridge, queries); }
  let entry = queries.get(key);
  if (!entry) {
    const queryId = `shared-${Date.now()}-${++sequence}`;
    const cancellable = name === 'stats.explore' || name === 'stats.summary' || name === 'stats.evidence' || name === 'stats.outputReferences';
    const request = { ...normalized, ...(cancellable ? { queryId } : {}) } as RequestMap[K];
    entry = { promise: Promise.resolve(undefined), readers: new Set(), done: false,
      cancel: () => { if (cancellable) void bridge.call('stats.cancelAnalysis', { queryId }).catch(() => {}); } };
    queries.set(key, entry);
    const current = entry;
    current.promise = bridge.call(name, request).then(response => {
      if (!response.ok) throw new AppError(response.error);
      return response.data;
    }).finally(() => {
      current.done = true;
      if (queries!.get(key) === current) queries!.delete(key);
    });
  }
  const current = entry, reader = Symbol(); current.readers.add(reader);
  return new Promise<CommandData<K>>((resolve, reject) => {
    let ended = false;
    function leave() {
      if (ended) return false;
      ended = true; signal.removeEventListener('abort', abort); current.readers.delete(reader);
      if (!current.done && !current.readers.size) {
        if (queries!.get(key) === current) queries!.delete(key);
        current.cancel();
      }
      return true;
    }
    function abort() { if (leave()) reject(cancelled()); }
    signal.addEventListener('abort', abort, { once: true });
    current.promise.then(value => { if (leave()) resolve(value as CommandData<K>); }, error => { if (leave()) reject(error); });
    if (signal.aborted) abort();
  });
}
