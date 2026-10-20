import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi, type MockInstance } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });
async function render() {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = mount(OverviewPage, { props: { initialPreferences }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); });
  await flushPromises(); return { bridge, call, wrapper };
}
function summaries(call: MockInstance<Bridge['call']>) { return call.mock.calls.filter(([name]) => name === 'stats.summary'); }
it('renders all-source metrics and reconciled breakdowns without Harness-only sections', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const { call, wrapper } = await render();
  expect(summaries(call)).toHaveLength(1); expect(summaries(call)[0]![1]).toMatchObject({ harness: 'all' });
  expect(wrapper.findAll('.metrics-grid .metric')).toHaveLength(6);
  expect(wrapper.find('.source-note').exists()).toBe(false);
  expect(wrapper.get('[aria-label="按来源汇总"]').findAll('tbody tr')).toHaveLength(4);
  expect(wrapper.get('[aria-label="按来源汇总"]').findAll('tbody a').map(link => link.attributes('href')).sort()).toEqual(['#/harness/claude', '#/harness/codex', '#/harness/dsh', '#/harness/zcode']);
  expect(wrapper.find('.balance-card').exists()).toBe(false); expect(wrapper.find('.cache-monitor').exists()).toBe(false);
  expect(wrapper.find('[aria-label="模型明细"]').exists()).toBe(true);
});
it('advances presets for any source event, defers hidden queries and refreshes once on restoration', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const { bridge, call } = await render();
  vi.setSystemTime(new Date('2026-10-06T08:05:00Z'));
  bridge.receiveEvent('usage.changed', { harnesses: ['dsh'], revision: 2 }); await flushPromises();
  expect(summaries(call)).toHaveLength(2); expect(summaries(call)[1]![1]).toMatchObject({ end: '2026-10-06T08:05:00.000Z' });
  bridge.receiveEvent('app.visibility', { visible: false });
  for (let revision = 3; revision < 8; revision++) bridge.receiveEvent('usage.changed', { harnesses: ['zcode'], revision });
  await flushPromises(); expect(summaries(call)).toHaveLength(2);
  vi.setSystemTime(new Date('2026-10-06T08:10:00Z'));
  bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises();
  expect(summaries(call)).toHaveLength(3); expect(summaries(call)[2]![1]).toMatchObject({ end: '2026-10-06T08:10:00.000Z' });
});
it('persists overview filters independently and refreshes stats and collection after a scan', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const { bridge, call, wrapper } = await render();
  await wrapper.get('[aria-label^="时间范围："]').trigger('click');
  await wrapper.get('[role="radio"][data-value="7days"]').trigger('click'); await flushPromises();
  const preferences = unwrap(await bridge.call('preferences.get', {}));
  expect(preferences.preferences.filters.overview?.range).toBe('7days');
  expect(preferences.preferences.filters.codex).toBeUndefined();
  const before = summaries(call).length;
  await wrapper.get('.scan-control button').trigger('click'); await vi.advanceTimersByTimeAsync(150); await flushPromises();
  expect(summaries(call).length).toBeGreaterThan(before);
  expect(wrapper.text()).toContain('监测中');
  expect(call.mock.calls.some(([name]) => name === 'collection.scanNow')).toBe(true);
});

it('filters every analysis section by source, remembers the choice and ignores unrelated source updates', async () => {
  const { bridge, call, wrapper } = await render();
  await wrapper.get('[aria-label^="数据来源："]').trigger('click');
  expect(wrapper.findAll('[aria-label="数据来源"] [role="radio"]').map(option => option.attributes('data-value'))).toEqual(['all', 'claude', 'codex', 'zcode', 'dsh']);
  await wrapper.get('[aria-label="数据来源"] [data-value="codex"]').trigger('click'); await flushPromises();
  expect(summaries(call).at(-1)![1]).toMatchObject({ harness: 'codex' });
  expect(wrapper.find('[aria-label="按来源汇总"]').exists()).toBe(false);
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.filters.overview?.harness).toBe('codex');
  const count = summaries(call).length;
  bridge.receiveEvent('usage.changed', { harnesses: ['dsh'], revision: 3 }); await flushPromises();
  expect(summaries(call)).toHaveLength(count);
  bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 4 }); await flushPromises();
  expect(summaries(call)).toHaveLength(count + 1);
  await wrapper.get('[aria-label^="数据来源："]').trigger('click');
  await wrapper.get('[aria-label="数据来源"] [data-value="all"]').trigger('click'); await flushPromises();
  expect(wrapper.find('[aria-label="按来源汇总"]').exists()).toBe(true);
});

it('changes trend metrics and the model table locally without starting a new rolling summary', async () => {
  const { call, wrapper } = await render();
  const before = summaries(call).length;
  await wrapper.get('[data-value="cost"]').trigger('click'); await flushPromises();
  await wrapper.get('[data-value="hitRate"]').trigger('click'); await flushPromises();
  expect(summaries(call)).toHaveLength(before);
  await wrapper.findAll('button').find(b => b.text() === '查看小时明细')!.trigger('click'); await flushPromises();
  const selector = wrapper.get('[aria-label="小时明细模型"]');
  await selector.setValue('0'); await flushPromises();
  expect(summaries(call)).toHaveLength(before + 2);
  expect(summaries(call).at(-1)?.[1]).toMatchObject({ sections: ['hourly'] });
});
