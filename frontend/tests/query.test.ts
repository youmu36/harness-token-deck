import { effectScope, isReactive, isReadonly, ref } from 'vue';
import type { EffectScope } from 'vue';
import { flushPromises } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppError } from '../src/app/errors';
import { createEventHub } from '../src/shared/events';
import { useQuery } from '../src/shared/query';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const scopes: EffectScope[] = [];
const hubs: ReturnType<typeof createEventHub>[] = [];
function fixture() {
  const scope = effectScope();
  const hub = createEventHub();
  scopes.push(scope);
  hubs.push(hub);
  return { scope, hub };
}
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); hubs.splice(0).forEach(hub => hub.dispose()); vi.useRealTimers(); });

it('keeps large snapshots shallow and defers passive events while explicit and restored reads stay immediate', async () => {
  vi.useFakeTimers();
  const { scope, hub } = fixture();
  const fetcher = vi.fn(async () => ({ rows: [{ amount: '123' }] }));
  const query = scope.run(() => useQuery('deferred', fetcher, { eventSource: hub.source, events: ['wallet.changed'], coalesce: true, eventIntervalMs: 3 * 3600_000 }))!;
  await flushPromises();
  expect(isReadonly(query.data)).toBe(true); expect(isReactive(query.data.value!.rows)).toBe(false);
  hub.emit('wallet.changed', { vendorIds: ['v'], revision: 1 }); await flushPromises();
  expect(fetcher).toHaveBeenCalledTimes(1);
  await query.refresh(); expect(fetcher).toHaveBeenCalledTimes(2);
  hub.emit('app.visibility', { visible: false });
  hub.emit('wallet.changed', { vendorIds: ['v'], revision: 2 });
  await vi.advanceTimersByTimeAsync(3 * 3600_000); expect(fetcher).toHaveBeenCalledTimes(2);
  hub.emit('app.visibility', { visible: true }); await flushPromises();
  expect(fetcher).toHaveBeenCalledTimes(3);
  hub.emit('wallet.changed', { vendorIds: ['v'], revision: 3 });
  scope.stop(); await vi.advanceTimersByTimeAsync(3 * 3600_000);
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it('releases closed detail snapshots and cancels pending results without retaining them on reopen', async () => {
  const {scope,hub}=fixture(), opened=ref(true), next=deferred<number[]>();
  const fetcher=vi.fn().mockResolvedValueOnce([1]).mockImplementationOnce(()=>next.promise).mockResolvedValue([3]);
  const query=scope.run(()=>useQuery('details',fetcher,{eventSource:hub.source,visible:opened,clearOnHide:true,coalesce:true}))!;
  await flushPromises();expect(query.data.value).toEqual([1]);
  opened.value=false;expect(query.data.value).toBeUndefined();
  opened.value=true;await flushPromises();expect(fetcher).toHaveBeenCalledTimes(2);
  const signal=fetcher.mock.calls[1]![0] as AbortSignal;
  opened.value=false;expect(signal.aborted).toBe(true);
  next.resolve([2]);await flushPromises();expect(query.data.value).toBeUndefined();
  opened.value=true;await flushPromises();expect(query.data.value).toEqual([3]);
  scope.stop();expect(query.data.value).toBeUndefined();
});

describe('useQuery', () => {
  it('keeps rows when a refreshed parent object has the same query identity', async () => {
    const { scope, hub } = fixture(); const vendor = ref({ id: 'vendor', balance: '10' });
    const fetcher = vi.fn().mockResolvedValue(['original-row']);
    const query = scope.run(() => useQuery('wallet.entries', fetcher, { eventSource: hub.source, deps: [() => vendor.value.id], clearOnChange: true }))!;
    await flushPromises();
    vendor.value = { id: 'vendor', balance: '20' }; await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(1); expect(query.data.value).toEqual(['original-row']);
    vendor.value = { id: 'other-vendor', balance: '0' }; expect(query.data.value).toBeUndefined();
    await flushPromises(); expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('coalesces statistics invalidations while one expensive request is running, then reads the newest snapshot once', async () => {
    const { scope, hub } = fixture(); const first = deferred<number>();
    const fetcher = vi.fn().mockImplementationOnce(() => first.promise).mockResolvedValue(2);
    const query = scope.run(() => useQuery('stats', fetcher, { eventSource: hub.source, events: ['usage.changed'], coalesce: true }))!;
    void query.refresh(); void query.refresh();
    for (let revision = 1; revision <= 10; revision++) hub.emit('usage.changed', { harnesses: ['codex'], revision });
    await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(1);
    first.resolve(1); await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2); expect(query.data.value).toBe(2);
  });
  it('coalesces a burst of visible events and immediately invalidates old responses', async () => {
    const { scope, hub } = fixture(); const first = deferred<number>();
    const fetcher = vi.fn().mockImplementationOnce(() => first.promise).mockResolvedValue(2);
    const query = scope.run(() => useQuery('status', fetcher, { eventSource: hub.source, events: ['sources.changed', 'collection.tick'] }))!;
    for (let revision = 1; revision <= 10; revision++) hub.emit('sources.changed', { revision });
    first.resolve(1); await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2); expect(query.data.value).toBe(2); expect(query.refreshing.value).toBe(false);
  });
  it('attaches repeated explicit refreshes without dirtying the same active query', async () => {
    const { scope, hub } = fixture(); const first = deferred<number>();
    const fetcher = vi.fn(() => first.promise);
    const query = scope.run(() => useQuery('stats', fetcher, { eventSource: hub.source, coalesce: true }))!;
    void query.refresh(); void query.refresh();
    first.resolve(1); await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(1); expect(query.data.value).toBe(1);
  });
  it('reruns changed parameters immediately after an active request despite passive throttling', async () => {
    vi.useFakeTimers();
    const { scope, hub } = fixture(); const first = deferred<string>(); const parameter = ref('A');
    let oldSignal: AbortSignal | undefined;
    const fetcher = vi.fn((signal: AbortSignal) => { oldSignal ??= signal; return parameter.value === 'A' ? first.promise : Promise.resolve(parameter.value); });
    const query = scope.run(() => useQuery('analysis', fetcher, {eventSource:hub.source,deps:[parameter],coalesce:true,clearOnChange:true,eventIntervalMs:86400000,events:['usage.changed']}))!;
    parameter.value = 'B'; await flushPromises();
    expect(oldSignal?.aborted).toBe(true);
    first.resolve('A'); await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2); expect(query.data.value).toBe('B'); expect(query.loading.value).toBe(false);
    hub.emit('usage.changed', {harnesses:['codex'],revision:2}); await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2);
    await query.refresh(); expect(fetcher).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });
  it.each(['event-first', 'completed-first'] as const)('reuses the scan event query when %s', async order => {
    const { scope, hub } = fixture();
    const fetcher = vi.fn().mockResolvedValue(1);
    const query = scope.run(() => useQuery('stats', fetcher, { eventSource: hub.source, events: ['usage.changed'], coalesce: true }))!;
    await flushPromises();
    const checkpoint = query.checkpoint();
    hub.emit('usage.changed', { harnesses: ['codex'], revision: 2 });
    if (order === 'event-first') await flushPromises();
    await query.refreshIfUnchanged(checkpoint); await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2);
    const noChange = query.checkpoint();
    await query.refreshIfUnchanged(noChange);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
  it('discards late successes after dependency changes', async () => {
    const { scope, hub } = fixture();
    const requests = [deferred<string>(), deferred<string>(), deferred<string>()];
    let index = 0;
    const parameter = ref('codex');
    const query = scope.run(() => useQuery('usage', () => requests[index++]!.promise, {
      deps: [parameter], eventSource: hub.source,
    }))!;
    expect(query.loading.value).toBe(true);
    parameter.value = 'zcode';
    requests[1]!.resolve('Zcode');
    await flushPromises();
    requests[0]!.resolve('Codex');
    await flushPromises();
    expect(query.data.value).toBe('Zcode');
    parameter.value = 'dsh';
    requests[2]!.resolve('Deepseek Harness');
    await flushPromises();
    expect(query.data.value).toBe('Deepseek Harness');
    expect(query.error.value).toBeUndefined();
  });

  it('keeps the previous snapshot during refresh and retryable failures', async () => {
    const { scope, hub } = fixture();
    const failure = deferred<number>();
    const fetcher = vi.fn().mockResolvedValueOnce(12).mockImplementationOnce(() => failure.promise).mockResolvedValueOnce(24);
    const query = scope.run(() => useQuery('cost', fetcher, { eventSource: hub.source }))!;
    await flushPromises();
    const refresh = query.refresh();
    expect(query.data.value).toBe(12);
    expect(query.refreshing.value).toBe(true);
    expect(query.loading.value).toBe(false);
    failure.reject(new AppError({ code: 'READ_TIMEOUT', message: '读取超时', retryable: true }));
    await refresh;
    expect(query.data.value).toBe(12);
    expect(query.error.value?.code).toBe('READ_TIMEOUT');
    await query.refresh();
    expect(query.data.value).toBe(24);
    expect(query.error.value).toBeUndefined();
  });

  it('pauses hidden queries and refreshes once after many invalidations', async () => {
    const { scope, hub } = fixture();
    const parameter = ref(1);
    const fetcher = vi.fn(async () => parameter.value);
    const query = scope.run(() => useQuery('summary', fetcher, {
      deps: [parameter], events: ['usage.changed'], eventSource: hub.source,
    }))!;
    await flushPromises();
    hub.emit('app.visibility', { visible: false });
    parameter.value = 2;
    for (let revision = 1; revision <= 10; revision++) hub.emit('usage.changed', { harnesses: ['codex'], revision });
    await query.refresh();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(query.data.value).toBe(1);
    expect(query.dirty.value).toBe(true);
    hub.emit('app.visibility', { visible: true });
    await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(query.data.value).toBe(2);
    expect(query.dirty.value).toBe(false);
    hub.emit('app.visibility', { visible: false });
    hub.emit('app.visibility', { visible: true });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('invalidates in-flight work on hide and ignores its late error', async () => {
    const { scope, hub } = fixture();
    const stale = deferred<string>();
    const fetcher = vi.fn().mockImplementationOnce(() => stale.promise).mockResolvedValueOnce('current');
    const query = scope.run(() => useQuery('summary', fetcher, { eventSource: hub.source }))!;
    hub.emit('app.visibility', { visible: false });
    stale.reject(new Error('stale'));
    await flushPromises();
    expect(query.loading.value).toBe(false);
    expect(query.error.value).toBeUndefined();
    hub.emit('app.visibility', { visible: true });
    await flushPromises();
    expect(query.data.value).toBe('current');
  });

  it('filters events, respects page visibility, and clears sensitive snapshots on parameter changes', async () => {
    const { scope, hub } = fixture();
    const pageVisible = ref(true);
    const vendor = ref('A');
    const fetcher = vi.fn(async () => vendor.value);
    const query = scope.run(() => useQuery('balance', fetcher, {
      eventSource: hub.source, visible: pageVisible, deps: [vendor], clearOnChange: true,
      events: [{ name: 'wallet.changed', when: payload => payload.vendorIds.includes(vendor.value) }],
    }))!;
    await flushPromises();
    hub.emit('wallet.changed', { vendorIds: ['other'], revision: 1 });
    expect(fetcher).toHaveBeenCalledTimes(1);
    hub.emit('wallet.changed', { vendorIds: ['A'], revision: 2 });
    await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2);
    pageVisible.value = false;
    vendor.value = 'B';
    expect(query.data.value).toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(2);
    pageVisible.value = true;
    await flushPromises();
    expect(query.data.value).toBe('B');
  });

  it('unsubscribes on disposal and never publishes disposed requests', async () => {
    const { scope, hub } = fixture();
    const request = deferred<number>();
    const fetcher = vi.fn(() => request.promise);
    const query = scope.run(() => useQuery('disposed', fetcher, { events: ['pricing.changed'], eventSource: hub.source }))!;
    scope.stop();
    request.resolve(100);
    hub.emit('pricing.changed', { revision: 1 });
    await query.refresh();
    await flushPromises();
    expect(query.data.value).toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
