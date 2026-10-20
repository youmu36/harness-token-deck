import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { UsageNavigationKey } from '../src/app/usageNavigation';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import { defaultFilter } from '../src/features/usage-common/types';
import HarnessPage from '../src/features/harness/HarnessPage.vue';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import VersionList from '../src/features/pricing/VersionList.vue';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); vi.useRealTimers(); });

it.each([72 - 1 / 60, 72, 72 + 1 / 60, 73, 168, 168 + 1 / 60, 169])('custom %s-hour analysis uses the permitted trend grain and defers hour details', async hours => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const initial = unwrap(await bridge.call('preferences.get', {}));
  initial.preferences.filters.overview = { ...defaultFilter, range: 'custom', start: '2026-10-01T00:00:00.000Z', end: new Date(Date.parse('2026-10-01T00:00:00Z') + hours * 3600000).toISOString() };
  const wrapper = mount(OverviewPage, { props: { initialPreferences: initial }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  const query = call.mock.calls.filter(([name]) => name === 'stats.summary');
  expect(query).toHaveLength(1);
  expect(query[0]![1]).toMatchObject({ grain: hours < 72 ? 'hour' : hours <= 168 ? 'twoHours' : 'day', sections: ['trend', 'models', 'harnesses'] });
  expect(wrapper.find('[aria-label="趋势粒度"] [data-value="hour"]').exists()).toBe(hours <= 168);
  if (hours >= 72 && hours <= 168) expect(wrapper.get('.grain-note').text()).toBe('实际粒度：2小时');
  else expect(wrapper.find('.grain-note').exists()).toBe(false);
});

it.each(['7days', '30days'] as const)('%s analysis offers the trend grains supported by its actual span', async range => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const initial = unwrap(await bridge.call('preferences.get', {}));
  initial.preferences.filters.overview = { ...defaultFilter, range };
  const wrapper = mount(OverviewPage, { props: { initialPreferences: initial }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  expect(call.mock.calls.find(([name]) => name === 'stats.summary')![1]).toMatchObject({ grain: range === '7days' ? 'twoHours' : 'day' });
  expect(wrapper.find('[aria-label="趋势粒度"] [data-value="hour"]').exists()).toBe(range === '7days');
  if (range === '7days') {
    await wrapper.get('[aria-label="趋势粒度"] [data-value="day"]').trigger('click'); await flushPromises();
    expect(call.mock.calls.filter(([name]) => name === 'stats.summary').at(-1)![1]).toMatchObject({ grain: 'day' });
  }
});

it.each([168, 169])('Harness %s-hour range keeps totals and routes only long hour details', async hours => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call'); const analyze = vi.fn();
  const initial = unwrap(await bridge.call('preferences.get', {}));
  initial.preferences.filters.codex = { ...defaultFilter, range: 'custom', start: '2026-10-01T00:00:00.000Z', end: new Date(Date.parse('2026-10-01T00:00:00Z') + hours * 3600000).toISOString(), modelKey: 'gpt-6' };
  const wrapper = mount(HarnessPage, { props: { harness: 'codex', initialPreferences: initial }, global: { provide: { [BridgeKey as symbol]: bridge, [UsageNavigationKey as symbol]: { analyze, back: vi.fn() } } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.summary')).toHaveLength(1);
  expect(wrapper.find('.history-link').exists()).toBe(hours > 168);
  expect(wrapper.find('[aria-label="模型明细"]').exists()).toBe(true);
  if (hours > 168) {
    await wrapper.get('.history-link button').trigger('click');
    expect(analyze).toHaveBeenCalledWith(expect.objectContaining({ harness: 'codex', range: 'custom', modelKey: 'gpt-6', start: '2026-10-01T00:00:00.000Z' }));
  }
});

it('version actions protect the active version and offer enable/disable for other versions', async () => {
  const bridge = createMockBridge(); const group = unwrap(await bridge.call('pricing.catalog', {})).groups[0]!;
  group.versions[1]!.disabled = true;
  const wrapper = mount(VersionList, { props: { group, mode: 'UTC' } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); });
  const active = wrapper.findAll('.version').find(row => row.text().includes('当前生效'))!;
  expect(active.findAll('button').find(button => button.text() === '删除')!.attributes('disabled')).toBeDefined();
  const disabled = wrapper.findAll('.version').find(row => row.text().includes('已停用'))!;
  await disabled.findAll('button').find(button => button.text() === '启用')!.trigger('click');
  expect(wrapper.emitted('disable')).toHaveLength(1);
});

it.each(['harness', 'overview'] as const)('%s scan events and completion perform one summary query', async page => {
  vi.useFakeTimers();
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const initial = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = page === 'harness'
    ? mount(HarnessPage, { props: { harness: 'codex', initialPreferences: initial }, global: { provide: { [BridgeKey as symbol]: bridge } } })
    : mount(OverviewPage, { props: { initialPreferences: initial }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  const before = call.mock.calls.filter(([name]) => name === 'stats.summary').length;
  await wrapper.get('.scan-control button').trigger('click'); await vi.advanceTimersByTimeAsync(150); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.summary')).toHaveLength(before + 1);
  expect(call.mock.calls.filter(([name]) => name === 'collection.scanNow')).toHaveLength(1);
});
