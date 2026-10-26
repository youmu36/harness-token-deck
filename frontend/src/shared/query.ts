import { computed, inject, onScopeDispose, readonly, ref, shallowReadonly, shallowRef, toValue, watch } from 'vue';
import type { MaybeRefOrGetter, WatchSource } from 'vue';
import { BridgeKey } from '../app/ports';
import type { EventMap, EventName, EventSource } from '../app/ports';
import { asAppError } from '../app/errors';
import type { AppError } from '../app/errors';

export type QueryEvent = {
  [K in EventName]: { name: K; when?: (payload: EventMap[K]) => boolean; immediate?: boolean }
}[EventName] | EventName;

interface QueryOptions {
  deps?: WatchSource[];
  events?: QueryEvent[];
  eventSource?: EventSource;
  visible?: MaybeRefOrGetter<boolean>;
  clearOnChange?: boolean;
  /** Release reconstructible detail snapshots when their view closes. */
  clearOnHide?: boolean;
  /** Dataset identity; rolling time updates may refresh without blanking existing rows. */
  identity?: MaybeRefOrGetter<unknown>;
  coalesce?: boolean;
  eventIntervalMs?: number;
}

// A query owns its generation; hidden requests never publish stale snapshots.
export function useQuery<T>(key: string, fetcher: (signal: AbortSignal) => Promise<T>, options: QueryOptions = {}) {
  const source = options.eventSource ?? inject(BridgeKey)?.events;
  if (!source) throw new Error('useQuery requires an application event source');
  const data = shallowRef<T>();
  const error = shallowRef<AppError>();
  const pending = ref(false);
  const dirty = ref(true);
  const hostVisible = ref(source.isVisible());
  const visible = computed(() => hostVisible.value && toValue(options.visible ?? true));
  let generation = 0;
  let requests = 0;
  let disposed = false;
  let scheduled = false;
  let active: Promise<void> | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastStarted = -Infinity;
  let urgent = false;
  let controller: AbortController | undefined;
  let identity = toValue(options.identity);

  function cancelTimer() { if (timer !== undefined) { clearTimeout(timer); timer = undefined; } }

  function schedule() {
    if (scheduled || timer !== undefined || disposed || !visible.value) return;
    const delay = urgent ? 0 : Math.max(0, (options.eventIntervalMs ?? 0) - (Date.now() - lastStarted));
    if (delay) {
      timer = setTimeout(() => { timer = undefined; schedule(); }, delay);
      return;
    }
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      if (dirty.value && (!options.coalesce || !pending.value) && !disposed) void start();
    });
  }

  function invalidate(immediate = false) {
    ++requests;
    if (!options.coalesce) ++generation;
    dirty.value = true;
    if (immediate) { cancelTimer(); urgent = true; }
    schedule();
  }

  function refresh(): Promise<void> {
    if (options.coalesce && active && !dirty.value) return active;
    urgent = true;
    return start();
  }

  function start(): Promise<void> {
    cancelTimer();
    if (options.coalesce && active) return active;
    dirty.value = true;
    ++requests;
    const task = perform();
    active = task;
    void task.finally(() => {
      if (active !== task) return;
      active = undefined;
      if (dirty.value && visible.value && !disposed) schedule();
    });
    return task;
  }

  async function perform(): Promise<void> {
    const current = ++generation;
    dirty.value = true;
    if (disposed || !visible.value) return;
    lastStarted = Date.now();
    urgent = false;
    dirty.value = false;
    pending.value = true;
    error.value = undefined;
    try {
      controller?.abort();
      controller = new AbortController();
      const result = await fetcher(controller.signal);
      if (current === generation && !disposed && !dirty.value) data.value = result;
    } catch (failure) {
      if (current === generation && !disposed && !dirty.value) error.value = asAppError(failure);
    } finally {
      if (current === generation && !disposed) { pending.value = false; }
    }
  }

  const cleanups = [source.onVisibility(value => { hostVisible.value = value; })];
  for (const event of options.events ?? []) {
    const subscription = typeof event === 'string' ? { name: event } : event;
    cleanups.push(source.subscribe(subscription.name, payload => {
      // The event name selects the payload type at the public API boundary.
      const accepts = 'when' in subscription
        ? subscription.when as ((value: typeof payload) => boolean) | undefined
        : undefined;
      if (!accepts || accepts(payload)) invalidate('immediate' in subscription && subscription.immediate === true);
    }));
  }
  watch(visible, value => {
    if (value) {
      if (dirty.value) void refresh();
    } else {
      cancelTimer();
      controller?.abort();
      ++generation;
      if (pending.value) dirty.value = true;
      pending.value = false;
      if (options.clearOnHide) { data.value = undefined; dirty.value = true; }
    }
  }, { immediate: true, flush: 'sync' });
  if (options.deps?.length) watch(options.deps, () => {
    controller?.abort();
    ++requests;
    dirty.value = true;
    const nextIdentity = toValue(options.identity);
    if (options.clearOnChange && (options.identity === undefined || !Object.is(identity, nextIdentity))) data.value = undefined;
    identity = nextIdentity;
    void refresh();
  }, { flush: 'sync' });

  onScopeDispose(() => {
    disposed = true;
    controller?.abort();
    cancelTimer();
    ++generation;
    pending.value = false;
    data.value = undefined;
    error.value = undefined;
    cleanups.forEach(cleanup => cleanup());
  });
  return {
    key, data: shallowReadonly(data), error: readonly(error), dirty: readonly(dirty),
    loading: computed(() => pending.value && data.value === undefined),
    refreshing: computed(() => pending.value && data.value !== undefined),
    refresh, checkpoint: () => requests,
    refreshIfUnchanged: (checkpoint: number) => requests === checkpoint || (dirty.value && !active) ? refresh() : active ?? Promise.resolve(),
  };
}
