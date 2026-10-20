import { inject, onScopeDispose, toValue, type MaybeRefOrGetter } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import type { TimeZoneMode } from '../../shared/time';
import type { Harness } from './types';

const minute = 60_000;
const hour = 60 * minute;
export function monitorOffset(mode: 'minute' | 'hour', timezone: TimeZoneMode, now: number) {
  if (timezone === 'UTC') return 0;
  const end = Math.floor(now / minute) * minute;
  const offset = -new Date(end - 1).getTimezoneOffset();
  const start = mode === 'minute' ? end - hour : Math.floor((end - 1 + offset * minute) / hour) * hour - offset * minute - 23 * hour;
  return -new Date(start).getTimezoneOffset();
}

export function useCacheMonitor(harness: MaybeRefOrGetter<Harness>, mode: MaybeRefOrGetter<'minute' | 'hour'>,
  timezone: MaybeRefOrGetter<TimeZoneMode>, clock = Date.now) {
  const bridge = inject(BridgeKey)!;
  let lastWindow = '';
  const windowKey = () => `${toValue(harness)}:${toValue(mode)}:${toValue(timezone)}:${Math.floor(clock() / minute)}`;
  const query = useQuery('harness.cacheMonitor', async () => {
    lastWindow = windowKey();
    return unwrap(await bridge.call('stats.cacheMonitor', { harness: toValue(harness), mode: toValue(mode), offsetMinutes: monitorOffset(toValue(mode), toValue(timezone), clock()) }));
  }, {
    deps: [() => toValue(harness), () => toValue(mode), () => toValue(timezone)],
    events: [{ name: 'usage.changed', when: event => event.harnesses.includes(toValue(harness)) }],
    clearOnChange: true, coalesce: true, eventIntervalMs: 5_000,
  });
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
