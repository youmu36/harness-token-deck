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
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = ''; });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
function renderMonitor(bridge: Bridge, mode: 'minute' | 'hour' = 'minute') {
  const wrapper = mount(CacheMonitor, { props: { harness: 'codex', mode, timezone: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); return wrapper;
}
const calls = (call: { mock: { calls: unknown[][] } }, name: string) => call.mock.calls.filter(([command]) => command === name);

it('lists newest sessions first in both menus while keeping their chronological numbers',async () => {
  const bridge=mock(),call=vi.spyOn(bridge,'call'),wrapper=renderMonitor(bridge);await flushPromises();
  const before=calls(call,'stats.cacheMonitor').length;
  await wrapper.get('.session-info .choice-trigger').trigger('click');
  expect(wrapper.findAll('.session-info [role="radio"]').map(row=>row.text().match(/^会话\d+/)?.[0])).toEqual(['会话3','会话2','会话1']);
  await wrapper.get('.session-info .choice-trigger').trigger('click');
  await wrapper.get('.chart-tools .choice-trigger').trigger('click');
  expect(wrapper.findAll('.chart-tools [role="radio"]').map(row=>row.text().match(/^会话\d+/)?.[0])).toEqual(['会话3','会话2','会话1']);
  expect(calls(call,'stats.cacheMonitor')).toHaveLength(before);
});

it('pins the first loaded chart, remembers manual selection and resets only when the session expires', async () => {
  const bridge=mock(), original=bridge.call.bind(bridge);
  let phase=0;
  const call=vi.spyOn(bridge,'call').mockImplementation(async(name,payload) => {
    const response=await original(name,payload);
    if(name!=='stats.cacheMonitor' || !response.ok)return response;
    const data=response.data as CommandData<'stats.cacheMonitor'>;
    const sessions=phase===0 ? data.sessionHitRates.slice(0,1) : phase===1 ? data.sessionHitRates.slice(0,2) : phase===3 ? data.sessionHitRates.filter((_,i)=>i!==1) : phase===4 ? [] : data.sessionHitRates;
    const requested=(payload as {chartSessionRef?:string}).chartSessionRef;
    const selected=sessions.find(row=>row.sessionRef===requested)?.sessionRef ?? sessions[0]?.sessionRef ?? null;
    return {ok:true,data:{...data,sessionHitRates:sessions,chartSessionRef:selected,selectedSessionRef:sessions.at(-1)?.sessionRef??null}} as never;
  });
  const wrapper=renderMonitor(bridge);await flushPromises();
  const trigger=()=>wrapper.get('.chart-tools .choice-trigger');
  expect(trigger().text()).toBe('柱状图视图：会话1');expect(calls(call,'stats.cacheMonitor')).toHaveLength(1);
  const first=localStorage.getItem('htd.cache-chart.codex');expect(first).toBeTruthy();
  phase=1;await wrapper.vm.refresh();await flushPromises();
  expect(trigger().text()).toBe('柱状图视图：会话1');expect(calls(call,'stats.cacheMonitor').at(-1)?.[1]).toMatchObject({chartSessionRef:first});
  await trigger().trigger('click');
  expect(wrapper.get('.chart-tools [role="radiogroup"]').text()).toContain('进行中');
  await wrapper.findAll('.chart-tools [role="radio"]').find(row=>row.text().startsWith('会话2 '))!.trigger('click');await flushPromises();
  const manual=localStorage.getItem('htd.cache-chart.codex');expect(manual).not.toBe(first);
  phase=2;await wrapper.vm.refresh();await flushPromises();expect(trigger().text()).toBe('柱状图视图：会话2');
  wrapper.unmount();const restored=renderMonitor(bridge);await flushPromises();
  expect(calls(call,'stats.cacheMonitor').at(-1)?.[1]).toMatchObject({chartSessionRef:manual});
  phase=3;await restored.vm.refresh();await flushPromises();expect(localStorage.getItem('htd.cache-chart.codex')).toBe(first);
  phase=2;await restored.vm.refresh();await flushPromises();
  const beforeReselect=calls(call,'stats.cacheMonitor').length;
  await restored.get('.chart-tools .choice-trigger').trigger('click');
  await restored.findAll('.chart-tools [role="radio"]').at(1)!.trigger('click');await flushPromises();
  expect(calls(call,'stats.cacheMonitor')).toHaveLength(beforeReselect+1);
  expect(calls(call,'stats.cacheMonitor').at(-1)?.[1]).toMatchObject({chartSessionRef:manual});
  phase=4;await restored.vm.refresh();await flushPromises();expect(localStorage.getItem('htd.cache-chart.codex')).toBeNull();
  expect(restored.get('.chart-tools .choice-trigger').text()).toBe('柱状图视图：无会话');
});

it('keeps chart selection independent of the metric session and remembers it after remount',async()=>{
  const bridge=mock(),call=vi.spyOn(bridge,'call'),wrapper=renderMonitor(bridge);await flushPromises();
  const chart=wrapper.get('.chart-tools');await chart.get('.choice-trigger').trigger('click');
  await chart.findAll('[role="radio"]').at(0)!.trigger('click');await flushPromises();
  const saved=localStorage.getItem('htd.cache-chart.codex');expect(saved).toBeTruthy();
  const metric=wrapper.get('.session-info');await metric.get('.choice-trigger').trigger('click');
  await metric.findAll('[role="radio"]').at(1)!.trigger('click');await flushPromises();
  expect(calls(call,'stats.cacheMonitor').at(-1)?.[1]).toMatchObject({chartSessionRef:saved});
  expect(wrapper.text()).not.toContain('窗口命中');expect(wrapper.text()).not.toContain('全程输入');
  wrapper.unmount();renderMonitor(bridge);await flushPromises();
  expect(calls(call,'stats.cacheMonitor').at(-1)?.[1]).toMatchObject({chartSessionRef:saved,sessionRef:null});
});

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
  it('refreshes title costs only for their own revision, using metadata and a single wallet for the header', async()=>{
    const bridge=mock(),initialPreferences=unwrap(await bridge.call('preferences.get',{}));
    const call=vi.spyOn(bridge,'call');
    const wrapper=mount(HarnessPage,{props:{harness:'codex',initialPreferences},global:{provide:{[BridgeKey as symbol]:bridge}}});
    cleanup.push(()=>wrapper.unmount());await flushPromises();
    expect(calls(call,'pricing.catalog').at(-1)?.[1]).toEqual({metadataOnly:true});
    expect(calls(call,'wallet.list').at(-1)?.[1]).toHaveProperty('vendorId');
    const before=calls(call,'stats.auxiliaryCosts').length;
    bridge.receiveEvent('usage.changed',{harnesses:['codex'],revision:2});
    await vi.advanceTimersByTimeAsync(5000);expect(calls(call,'stats.auxiliaryCosts')).toHaveLength(before);
    bridge.receiveEvent('auxiliary.changed',{revision:1});await flushPromises();
    expect(calls(call,'stats.auxiliaryCosts')).toHaveLength(before+1);
  });
  it('omits placeholder bars and the extra empty message while retaining warnings', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge);
    let empty = false;
    vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
      const response = await original(name, payload);
      if (name !== 'stats.cacheMonitor' || !response.ok) return response;
      const result = structuredClone(response.data) as CommandData<'stats.cacheMonitor'>;
      if (empty) { result.trend = []; result.revision++; }
      else result.trend = [{ bucket: result.start, label: '10-06 05:32', cacheHitRate: null, cacheBust: true, cacheLoss: true, contextCompacted: false, inputTokens: 10_000, cacheReadTokens: 0, outputTokens: 0, recordCount: 1, picoUsd: '0' }];
      return { ok: true, data: result } as never;
    });
    const wrapper = renderMonitor(bridge); await flushPromises();
    expect(wrapper.find('rect.unknown').exists()).toBe(false);
    expect(wrapper.find('rect.cache-bust').exists()).toBe(true); expect(wrapper.find('rect.cache-loss').exists()).toBe(false);
    await wrapper.get('.point').trigger('focus');
    expect(wrapper.get('.chart-tooltip').text()).toContain('缓存击穿：上下文 ≥ 5 万且缓存命中率低于 10%');
    expect(wrapper.get('.cache-legend').text()).toContain('上下文 ≥ 5 万且命中率低于 10%');
    expect(wrapper.get('.cache-legend').text()).toContain('已识别的压缩单独说明');
    empty = true; await wrapper.vm.refresh(); await flushPromises();
    expect(wrapper.text()).not.toContain('最近 60 分钟暂无缓存数据');
    expect(wrapper.find('.bar-chart').exists()).toBe(true);
    expect(wrapper.find('foreignObject.bar').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('最近 24 小时');
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
    expect(calls(call, 'stats.summary')).toHaveLength(before);
    await vi.advanceTimersByTimeAsync(5000);
    expect(calls(call, 'stats.summary')).toHaveLength(before + 1);
    expect(calls(call, 'stats.cacheMonitor')).toHaveLength(3);
    expect(wrapper.find('[aria-label="缓存监控范围"]').exists()).toBe(false);
  });
  it('uses 60 complete buckets and advances the axis at an exact hour', async () => {
    const bridge = mock();
    const first = unwrap(await bridge.call('stats.cacheMonitor', { harness: 'codex', mode: 'minute', offsetMinutes: 480 }));
    expect(first.end).toBe('2026-10-06T06:32:00.000Z'); expect(first.trend).toHaveLength(60);
    vi.setSystemTime(new Date('2026-10-06T07:00:00.000Z'));
    const exact = unwrap(await bridge.call('stats.cacheMonitor', { harness: 'codex', mode: 'minute', offsetMinutes: 480 }));
    expect(exact.start).toBe('2026-10-06T06:00:00.000Z'); expect(exact.trend).toHaveLength(60);
    expect(exact.windowKey).not.toBe(first.windowKey);
    expect(monitorOffset('UTC', Date.now())).toBe(0);
    expect(monitorOffset('system', Date.now())).toBe(-new Date(Date.now() - 60 * 60_000).getTimezoneOffset());
  });
});
