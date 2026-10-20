import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BridgeKey, type Bridge, type CommandData } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import CacheMonitor from '../src/features/harness/CacheMonitor.vue';
import HarnessPage from '../src/features/harness/HarnessPage.vue';
import { monitorOffset } from '../src/features/harness/useCacheMonitor';

const cleanup: (() => void)[] = [];
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T06:32:17.000Z')); });
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); document.body.innerHTML = ''; });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
function renderMonitor(bridge: Bridge, mode: 'minute' | 'hour' = 'minute') {
  const wrapper = mount(CacheMonitor, { props: { harness: 'codex', mode, timezone: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); return wrapper;
}
const calls = (call: { mock: { calls: unknown[][] } }, name: string) => call.mock.calls.filter(([command]) => command === name);

describe('cache monitor clock', () => {
  it('aligns to the next complete minute and keeps its clock after a manual refresh', async () => {
    const bridge = mock(); const call = vi.spyOn(bridge, 'call'); const wrapper = renderMonitor(bridge);
    await flushPromises(); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(42_999); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(10_000); await wrapper.vm.refresh();
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(49_999); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(1); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(4);
    wrapper.unmount(); expect(vi.getTimerCount()).toBe(0);
  });
  it('does no hidden reads and restores once, including recovery inside the same minute', async () => {
    const bridge = mock(); const call = vi.spyOn(bridge, 'call'); renderMonitor(bridge); await flushPromises();
    bridge.receiveEvent('app.visibility', { visible: false }); await flushPromises();
    await vi.advanceTimersByTimeAsync(10_000); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
    bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises();
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(2);
    bridge.receiveEvent('app.visibility', { visible: false }); await flushPromises();
    await vi.advanceTimersByTimeAsync(3 * 60_000); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(2);
    bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises();
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(33_000); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(4);
  });
  it('defers initial loading while hidden and ignores collection/usage events', async () => {
    const bridge = mock(); bridge.receiveEvent('app.visibility', { visible: false });
    const call = vi.spyOn(bridge, 'call'); renderMonitor(bridge); await flushPromises();
    await vi.advanceTimersByTimeAsync(60_000); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(0);
    bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises();
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
    bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 2 });
    bridge.receiveEvent('collection.tick', { status: {}, nextPeriodicAt: null, lastCompletedAt: null, trigger: 'periodic' });
    await flushPromises(); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
  });
  it('does not repeat a clock window after an explicit refresh at the boundary', async () => {
    const bridge = mock(); const call = vi.spyOn(bridge, 'call'); const wrapper = renderMonitor(bridge); await flushPromises();
    vi.setSystemTime(new Date('2026-10-06T06:33:00.000Z')); await wrapper.vm.refresh();
    vi.setSystemTime(new Date('2026-10-06T06:32:17.000Z')); await vi.advanceTimersByTimeAsync(43_000);
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(2);
  });
});

describe('cache monitor and Harness integration', () => {
  it('renders unknown placeholders, independent warnings and both empty messages', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge);
    let empty = false;
    vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
      const response = await original(name, payload);
      if (name !== 'stats.cacheMonitor' || !response.ok) return response;
      const result = structuredClone(response.data) as CommandData<'stats.cacheMonitor'>;
      if (empty) result.trend = [];
      else result.trend = [{ bucket: result.start, label: '10-06 05:32', cacheHitRate: null, cacheBust: true, cacheLoss: true, contextCompacted: false, inputTokens: 10_000, cacheReadTokens: 0, outputTokens: 0, recordCount: 1, picoUsd: '0' }];
      return { ok: true, data: result } as never;
    });
    const wrapper = renderMonitor(bridge); await flushPromises();
    expect(wrapper.find('rect.unknown').exists()).toBe(true);
    expect(wrapper.find('rect.cache-bust').exists()).toBe(true); expect(wrapper.find('rect.cache-loss').exists()).toBe(false);
    await wrapper.get('.point').trigger('focus');
    expect(wrapper.get('.chart-tooltip').text()).toContain('缓存击穿：上下文 ≥ 5 万且缓存命中率低于 10%');
    expect(wrapper.get('.cache-legend').text()).toContain('上下文 ≥ 5 万且命中率低于 10%');
    expect(wrapper.get('.cache-legend').text()).toContain('已识别的压缩单独说明');
    empty = true; await wrapper.vm.refresh(); await flushPromises();
    expect(wrapper.text()).toContain('最近 60 分钟暂无缓存数据');
    await wrapper.setProps({ mode: 'hour' }); await flushPromises();
    expect(wrapper.text()).toContain('最近 24 小时暂无缓存数据');
    expect(wrapper.find('.chart-tooltip').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('当前范围暂无数据');
  });
  it('updates cache once after scan completion even when usage and tick events fire', async () => {
    const bridge = mock(); const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
    const call = vi.spyOn(bridge, 'call');
    const wrapper = mount(HarnessPage, { props: { harness: 'codex', initialPreferences }, global: { provide: { [BridgeKey as symbol]: bridge } } });
    cleanup.push(() => wrapper.unmount()); await flushPromises();
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
    await wrapper.get('.scan-control button').trigger('click');
    await vi.advanceTimersByTimeAsync(149); expect(calls(call, 'stats.cacheMonitor')).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1); await flushPromises();
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(2);
    expect(wrapper.get('.monitor-panel').text()).toContain('每分钟更新，统计至上一完整分钟');
    const before = calls(call, 'stats.summary').length;
    bridge.receiveEvent('usage.changed', { harnesses: ['zcode'], revision: 9 }); await flushPromises();
    expect(calls(call, 'stats.summary')).toHaveLength(before);
    bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 10 }); await flushPromises();
    expect(calls(call, 'stats.summary')).toHaveLength(before + 1);
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(2);
    await wrapper.get('[aria-label="缓存监控范围"] [data-value="hour"]').trigger('click'); await flushPromises();
    expect(unwrap(await bridge.call('preferences.get', {})).preferences.filters.codex?.monitorMode).toBe('hour');
  });
  it('preview windows end at a full minute and contain exactly 60 / 24 buckets at hour boundaries', async () => {
    const bridge = mock();
    const minute = unwrap(await bridge.call('stats.cacheMonitor', { harness: 'codex', mode: 'minute', offsetMinutes: 480 }));
    expect(minute.end).toBe('2026-10-06T06:32:00.000Z'); expect(minute.nextRefreshAt).toBe('2026-10-06T06:33:00.000Z');
    expect(minute.trend).toHaveLength(60);
    const hour = unwrap(await bridge.call('stats.cacheMonitor', { harness: 'codex', mode: 'hour', offsetMinutes: 480 }));
    expect(hour.trend).toHaveLength(24); expect(hour.start).toBe('2026-10-05T07:00:00.000Z');
    vi.setSystemTime(new Date('2026-10-06T07:00:00.000Z'));
    const exact = unwrap(await bridge.call('stats.cacheMonitor', { harness: 'codex', mode: 'hour', offsetMinutes: 480 }));
    expect(exact.start).toBe(hour.start); expect(exact.trend).toHaveLength(24); expect(exact.windowKey).not.toBe(hour.windowKey);
    expect(monitorOffset('hour', 'UTC', Date.now())).toBe(0);
    expect(monitorOffset('minute', 'system', Date.now())).toBe(-new Date(Date.now() - 60 * 60_000).getTimezoneOffset());
  });
});
