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

it('separates parallel sessions and shows both recent and lifetime speed without more queries', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'stats.cacheMonitor' && result.ok) Object.assign(result.data, { outputSpeeds: [
      {sessionKey:'1111111111111111',startedAt:'2026-10-18T00:00:00.000Z',sessionAverageTps:'20.0',modelKey:'a',displayName:'model-a',tokensPerSecond:'12.3',outputTokens:'738',activeMinutes:1,lastOutputAt:'2026-10-18T00:00:00.000Z',frozen:true,basis:'minute_estimate'},
      {sessionKey:'2222222222222222',startedAt:'2026-10-18T00:01:00.000Z',sessionAverageTps:'42.0',modelKey:'b',displayName:'model-b',tokensPerSecond:'48.6',outputTokens:'2916',activeMinutes:1,lastOutputAt:'2026-10-18T00:00:00.000Z',frozen:false,basis:'generation'},
      {sessionKey:'3333333333333333',startedAt:'2026-10-18T00:02:00.000Z',sessionAverageTps:'72.0',modelKey:'b',displayName:'model-b',tokensPerSecond:'80.0',outputTokens:'4800',activeMinutes:1,lastOutputAt:'2026-10-18T00:00:00.000Z',frozen:false,basis:'generation'},
    ], sessionHitRates: [
      {sessionKey:'1111111111111111',displayName:'model-a',startedAt:'2026-10-18T00:00:00.000Z',lastActivityAt:'2026-10-18T00:09:00.000Z',cacheHitRate:0.4,cacheCoverageIncomplete:false,frozen:false},
      {sessionKey:'2222222222222222',displayName:'model-b',startedAt:'2026-10-18T00:01:00.000Z',lastActivityAt:'2026-10-18T00:09:30.000Z',cacheHitRate:0.7,cacheCoverageIncomplete:true,frozen:false},
    ] });
    return result;
  });
  const wrapper = mount(CacheMonitor, {props:{harness:'codex',mode:'minute',timezone:'UTC'},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const box = () => wrapper.get('.output-speed');
  expect(box().element.parentElement?.className).toBe('monitor-tools');
  expect(box().get('.choice-trigger').text().replace(/\s+/g, ' ')).toBe('会话2 近似实时tps 48.6 t/s 监测中');
  expect(box().get('.choice-trigger').text().replace(/\s+/g, ' ')).not.toContain('model-b');
  expect(box().text().replace(/\s+/g, ' ')).not.toContain('仅统计正输出');
  expect(box().find('[role="radiogroup"]').exists()).toBe(false);
  const hitRate = wrapper.get('[aria-label="当前会话缓存命中率"]');
  expect(hitRate.get('.choice-trigger').text().replace(/\s+/g, ' ')).toContain('会话1 缓存命中 40.0% 监测中');
  expect(hitRate.get('[role=tooltip]').text().replace(/\s+/g, ' ')).not.toContain('40.0');
  const count = call.mock.calls.length;
  await box().get('.choice-trigger').trigger('click');
  expect(box().text().replace(/\s+/g, ' ')).toContain('model-b');
  expect(box().text().replace(/\s+/g, ' ')).toContain('48.6 t/s');
  expect(box().text().replace(/\s+/g, ' ')).toContain('会话3 model-b');
  expect(box().text().replace(/\s+/g, ' ')).toContain('80.0 t/s');
  expect(box().text().replace(/\s+/g, ' ')).toContain('会话平均tps 42.0 t/s');
  await box().findAll('[role="radio"]')[2]!.trigger('click');
  expect(box().get('.choice-trigger').text().replace(/\s+/g, ' ')).toBe('会话3 近似实时tps 80.0 t/s 监测中');
  expect(box().get('[role=tooltip]').text().replace(/\s+/g, ' ')).toContain('最近三个有效输出分钟');
  await hitRate.get('.choice-trigger').trigger('click');
  expect(hitRate.get('[role="radiogroup"]').text().replace(/\s+/g, ' ')).toContain('model-b');
  expect(hitRate.get('[role="radiogroup"]').text().replace(/\s+/g, ' ')).toContain('当前会话缓存命中率：70.0%');
  expect(call.mock.calls).toHaveLength(count);
  expect(wrapper.text().replace(/\s+/g, ' ')).toContain('已识别的压缩单独说明');
  expect(wrapper.text().replace(/\s+/g, ' ')).not.toContain('疑似');
});

it('splits facts and analysis and removes developer suggestions and redundant pages', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  vi.spyOn(bridge, 'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = mount(OverviewPage,{props:{initialPreferences},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.find('.metrics-grid').exists()).toBe(true);
  expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(false);
  await wrapper.findAll('[aria-label="统计分析子页"] button').find(button => button.text().replace(/\s+/g, ' ') === '数据分析')!.trigger('click'); await flushPromises();
  expect(wrapper.find('.metrics-grid').exists()).toBe(false);
  expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(true);
  expect(wrapper.find('.multi-line path').exists()).toBe(true);
  expect(wrapper.text().replace(/\s+/g, ' ')).not.toContain('分析建议');
  expect(wrapper.text().replace(/\s+/g, ' ')).not.toContain('周期比较');
  expect(wrapper.find('[aria-label="长期趋势"]').exists()).toBe(false);
  expect(wrapper.get('[aria-label="输出吞吐参考"]').text().replace(/\s+/g, ' ')).toContain('三十个业务日');
  expect(wrapper.get('[aria-label="筛选模型排名"]').text().replace(/\s+/g, ' ')).not.toContain('token/分钟');
});

it('uses today saved daily references and never falls back to a frozen session or other dates', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-21T08:00:00Z'));
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  let today = true;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'stats.outputReferences' && result.ok) Object.assign(result.data, { today:'2026-10-21', references:[{day:today ? '2026-10-21' : '2026-10-20',modelKey:'gpt-6',displayName:'GPT-6',tokensPerSecond:'12.3',samples:4}] });
    return result;
  });
  const options = unwrap(await bridge.call('stats.exploreOptions', {}));
  const wrapper = mount(ThroughputReference,{props:{options},global:{provide:{[BridgeKey as symbol]:bridge}}}); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.get('.daily-speed').text().replace(/\s+/g, ' ')).toContain('12.3');
  expect(wrapper.get('.daily-speed').text().replace(/\s+/g, ' ')).toContain('4 个已保存会话样本');
  today = false; bridge.receiveEvent('output.changed', {revision:2}); await flushPromises();
  expect(wrapper.get('.daily-speed').text().replace(/\s+/g, ' ')).toContain('今天尚无已保存会话样本');
  expect(wrapper.get('.daily-speed strong').text().replace(/\s+/g, ' ')).not.toContain('12.3');
  await wrapper.findAll('button').find(button => button.text().replace(/\s+/g, ' ') === '比较模型')!.trigger('click');
  const boxes = wrapper.findAll('.comparison-models input');
  for (const box of boxes) if (!box.attributes('disabled')) await box.setValue(true);
  expect(wrapper.text().replace(/\s+/g, ' ')).toContain('4 / 4');
  await wrapper.findAll('button').find(button => button.text().replace(/\s+/g, ' ') === '完成选择')!.trigger('click'); await flushPromises();
  expect(wrapper.findAll('.line-legend li')).toHaveLength(5);
});
