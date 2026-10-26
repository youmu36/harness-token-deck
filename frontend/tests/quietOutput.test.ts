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

it('shows session hit rate and TPS together and offers two-line lifetime summaries', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const call = vi.spyOn(bridge, 'call');
  const wrapper = mount(CacheMonitor, {props:{harness:'codex',timezone:'UTC'},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => wrapper.unmount()); await vi.dynamicImportSettled(); await flushPromises();
  const box = wrapper.get('.session-info');
  expect(box.text()).toContain('tps 24.5'); expect(wrapper.find('.monitor-totals').exists()).toBe(false);
  expect(wrapper.text()).not.toContain('来源完整性尚未确认');
  expect(wrapper.get('h2').text()).toBe('近期缓存监控');
  const before = call.mock.calls.filter(([name]) => name === 'stats.cacheMonitor').length;
  await box.get('.choice-trigger').trigger('click');
  expect(box.text()).toContain('会话平均tps 24.5');
  await box.findAll('[role="radio"]').at(-1)!.trigger('click'); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.cacheMonitor')).toHaveLength(before + 1);
  expect(wrapper.text()).toContain('已识别的压缩单独说明');
});

it('splits facts and analysis and removes developer suggestions and redundant pages', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  vi.spyOn(bridge, 'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = mount(OverviewPage,{props:{initialPreferences},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => wrapper.unmount()); await vi.dynamicImportSettled(); await flushPromises();
  expect(wrapper.find('.metrics-grid').exists()).toBe(true);
  expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(false);
  await wrapper.findAll('[aria-label="统计分析子页"] button').find(button => button.text().replace(/\s+/g, ' ') === '数据分析')!.trigger('click'); await vi.dynamicImportSettled(); await flushPromises();
  expect(wrapper.find('.metrics-grid').exists()).toBe(false);
  expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(true);
  expect(wrapper.find('.multi-line path').exists()).toBe(true);
  expect(wrapper.text().replace(/\s+/g, ' ')).not.toContain('分析建议');
  expect(wrapper.text().replace(/\s+/g, ' ')).not.toContain('周期比较');
  expect(wrapper.find('[aria-label="长期趋势"]').exists()).toBe(false);
  expect(wrapper.get('[aria-label="输出吞吐参考"]').text().replace(/\s+/g, ' ')).toContain('本页独立时间范围');
  expect(wrapper.find('[aria-label="主题模型表"]').exists()).toBe(false);
});

it('uses today saved daily references and never falls back to a frozen session or other dates', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-21T08:00:00Z'));
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  let today = true;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'stats.outputReferences' && result.ok) Object.assign(result.data, { today:'2026-10-21', todayReferences:today ? [{day:'2026-10-21',modelKey:'gpt-6',displayName:'GPT-6',tokensPerSecond:'12.3',samples:4}] : [], references:[{day:today ? '2026-10-21' : '2026-10-20',modelKey:'gpt-6',displayName:'GPT-6',tokensPerSecond:'12.3',samples:4}] });
    return result;
  });
  const options = unwrap(await bridge.call('stats.exploreOptions', {}));
  const wrapper = mount(ThroughputReference,{props:{options},global:{provide:{[BridgeKey as symbol]:bridge}}}); cleanup.push(() => wrapper.unmount()); await vi.dynamicImportSettled(); await flushPromises();
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
