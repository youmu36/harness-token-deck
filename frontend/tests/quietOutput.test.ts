import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import CacheMonitor from '../src/features/harness/CacheMonitor.vue';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import ThroughputReference from '../src/features/overview/ThroughputReference.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach(fn => fn()); vi.restoreAllMocks(); vi.useRealTimers(); });

it('shows concurrent model count inline and expands all active speeds without more queries', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'stats.cacheMonitor' && result.ok) Object.assign(result.data, { outputSpeeds: [
      {modelKey:'a',displayName:'model-a',tokensPerSecond:'12.3',outputTokens:'738',activeMinutes:1,lastOutputAt:'2026-10-18T00:00:00.000Z',frozen:true,basis:'minute_estimate'},
      {modelKey:'b',displayName:'model-b',tokensPerSecond:'48.6',outputTokens:'2916',activeMinutes:1,lastOutputAt:'2026-10-18T00:00:00.000Z',frozen:false,basis:'generation'},
      {modelKey:'c',displayName:'model-c',tokensPerSecond:'80.0',outputTokens:'4800',activeMinutes:1,lastOutputAt:'2026-10-18T00:00:00.000Z',frozen:false,basis:'generation'},
    ], sessionHitRates: [
      {sessionKey:'1111111111111111',displayName:'model-a',startedAt:'2026-10-18T00:00:00.000Z',lastActivityAt:'2026-10-18T00:09:00.000Z',cacheHitRate:0.4,cacheCoverageIncomplete:false,frozen:false},
      {sessionKey:'2222222222222222',displayName:'model-b',startedAt:'2026-10-18T00:01:00.000Z',lastActivityAt:'2026-10-18T00:09:30.000Z',cacheHitRate:0.7,cacheCoverageIncomplete:true,frozen:false},
    ] });
    return result;
  });
  const wrapper = mount(CacheMonitor, {props:{harness:'codex',mode:'minute',timezone:'UTC'},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const box = () => wrapper.get('[aria-label="近60分钟模型输出速度"]');
  expect(box().element.parentElement?.className).toBe('monitor-tools');
  expect(box().text()).toContain('2 模型');
  expect(box().text()).not.toContain('仅统计正输出');
  expect(wrapper.find('[aria-label="正在运行的模型TPS"]').exists()).toBe(false);
  const hitRate = wrapper.get('[aria-label="当前会话缓存命中率"]');
  expect(hitRate.text()).toContain('当前会话缓存命中率：40.0%');
  expect(hitRate.text()).toContain('2 会话');
  expect(hitRate.classes()).toContain('output-speed');
  const count = call.mock.calls.length;
  (box().element as HTMLDetailsElement).open = true;
  await box().trigger('toggle');
  expect(box().text()).toContain('model-b');
  expect(box().text()).toContain('48.6 t/s');
  expect(box().text()).toContain('model-c');
  expect(box().text()).toContain('80.0 t/s');
  expect(box().text()).not.toContain('12.3');
  (hitRate.element as HTMLDetailsElement).open = true;
  await hitRate.trigger('toggle');
  expect(wrapper.get('[aria-label="会话缓存命中率列表"]').text()).toContain('model-b');
  expect(wrapper.get('[aria-label="会话缓存命中率列表"]').text()).toContain('70.0%');
  expect(call.mock.calls).toHaveLength(count);
  expect(wrapper.text()).toContain('已识别的压缩单独说明');
  expect(wrapper.text()).not.toContain('疑似');
});

it('splits facts and analysis and removes developer suggestions and redundant pages', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  vi.spyOn(bridge, 'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = mount(OverviewPage,{props:{initialPreferences},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.find('.metrics-grid').exists()).toBe(true);
  expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(false);
  await wrapper.findAll('[aria-label="统计分析子页"] button').find(button => button.text() === '数据分析')!.trigger('click'); await flushPromises();
  expect(wrapper.find('.metrics-grid').exists()).toBe(false);
  expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(true);
  expect(wrapper.find('.line-series').exists()).toBe(true);
  expect(wrapper.text()).not.toContain('分析建议');
  expect(wrapper.text()).not.toContain('周期比较');
  expect(wrapper.find('[aria-label="长期趋势"]').exists()).toBe(false);
  expect(wrapper.get('[aria-label="输出吞吐参考"]').text()).toContain('最近 30');
  expect(wrapper.get('[aria-label="模型长期排名"]').text()).not.toContain('token/分钟');
});

it('never uses combined model totals for the throughput reference', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const snapshot = unwrap(await bridge.call('stats.analytics', {period:'month'}));
  snapshot.models[0]!.tokensPerSecond = '12.30'; snapshot.totals.tokensPerSecond = '999.00';
  const wrapper = mount(ThroughputReference,{props:{snapshot}}); cleanup.push(() => wrapper.unmount());
  expect(wrapper.text()).toContain('tps：12.3 t/s'); expect(wrapper.text()).not.toContain('999');
  expect(wrapper.find('path.line-series').exists()).toBe(true);
});
