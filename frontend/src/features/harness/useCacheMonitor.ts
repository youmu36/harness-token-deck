import { inject, onScopeDispose, ref, toValue, watch, type MaybeRefOrGetter } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import type { TimeZoneMode } from '../../shared/time';
import type { Harness } from './types';

const minute = 60_000;
let sequence = 0;
export function monitorOffset(timezone: TimeZoneMode, now: number) {
  return timezone === 'UTC' ? 0 : -new Date(Math.floor(now / minute) * minute - 60 * minute).getTimezoneOffset();
}

export function useCacheMonitor(harness: MaybeRefOrGetter<Harness>, timezone: MaybeRefOrGetter<TimeZoneMode>,
  session: MaybeRefOrGetter<string | null>, cursor: MaybeRefOrGetter<string | null>, clock = Date.now,
  chartSession: MaybeRefOrGetter<string | null> = null) {
  const bridge = inject(BridgeKey)!;
  const chartRevision = ref(0);
  let lastWindow = '';
  const windowKey = () => `${toValue(harness)}:${toValue(session)}:${toValue(timezone)}:${Math.floor(clock() / minute)}`;
  const query = useQuery('harness.cacheMonitor', async signal => {
    lastWindow = windowKey();
    const queryId = `monitor-${Date.now()}-${++sequence}`;
    const cancel = () => { void bridge.call('stats.cancelAnalysis', { queryId }).catch(() => {}); };
    signal.addEventListener('abort', cancel, { once: true });
    try {
      return unwrap(await bridge.call('stats.cacheMonitor', { harness: toValue(harness), mode: 'minute', offsetMinutes: monitorOffset(toValue(timezone), clock()), sessionRef: toValue(session), chartSessionRef: toValue(chartSession) ?? '', cursor: toValue(cursor), queryId }));
    } finally { signal.removeEventListener('abort', cancel); }
  }, {
    deps: [() => toValue(harness), () => toValue(session), chartRevision, () => toValue(cursor), () => toValue(timezone)],
    events: [{ name: 'usage.changed', when: event => event.harnesses.includes(toValue(harness)) }, 'output.changed'],
    clearOnChange: true, coalesce: true, eventIntervalMs: 5_000,
  });
  watch(() => toValue(chartSession), value => {
    // A server-selected default is already loaded; only a user change needs a read.
    if (value !== query.data.value?.chartSessionRef) chartRevision.value++;
  }, { flush:'sync' });
  let timer: ReturnType<typeof setTimeout> | undefined;
  function stop() { if (timer !== undefined) clearTimeout(timer); timer = undefined; }
  function schedule() {
    stop(); if (!bridge.events.isVisible()) return;
    timer = setTimeout(() => { if (bridge.events.isVisible() && lastWindow !== windowKey()) void query.refresh(); schedule(); }, minute - (clock() % minute));
  }
  const unsubscribe = bridge.events.onVisibility(visible => { if (!visible) { stop(); void query.refresh(); } else { schedule(); } });
  schedule(); onScopeDispose(() => { stop(); unsubscribe(); });
  return query;
}
