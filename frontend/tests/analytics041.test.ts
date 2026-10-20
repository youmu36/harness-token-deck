import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type CommandData } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import AccountEntries from '../src/features/wallet/AccountEntries.vue';
import VendorBalanceCard from '../src/features/harness/VendorBalanceCard.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });

it('loads long history and model analysis on demand, without supplier queries or hidden summaries', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-18T10:00:00Z'));
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = mount(OverviewPage, { props: { initialPreferences }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  expect(call.mock.calls.some(([name]) => name === 'wallet.list' || name === 'stats.longTerm' || name === 'stats.analytics')).toBe(false);
  expect(wrapper.text()).not.toContain('供应商累计消费');
  await wrapper.get('[aria-label^="分析周期："]').trigger('click');
  await wrapper.get('[data-value="all"][role="radio"]').trigger('click'); await flushPromises();
  expect(call.mock.calls.find(([name]) => name === 'stats.analytics')?.[1]).toMatchObject({ period: 'all', harness: 'all', limit: 50 });
  expect(wrapper.find('[aria-label="长期来源结构"]').exists()).toBe(true);
  const count = call.mock.calls.filter(([name]) => name === 'stats.summary').length;
  bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 9 }); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.summary')).toHaveLength(count);
  await wrapper.findAll('[aria-label="统计分析子页"] button').find(b => b.text() === '数据分析')!.trigger('click'); await flushPromises();
  expect(wrapper.get('[aria-label="输出吞吐参考"]').text()).toContain('不代表真实生成速度');
  expect(wrapper.find('[aria-label="长期来源结构"]').exists()).toBe(false);
  await wrapper.get('[aria-label="比较 gpt-6"]').setValue(true);
  expect(wrapper.get('[aria-label="模型比较"]').text()).toContain('gpt-6');
  await wrapper.findAll('[aria-label="模型长期排名"] button').find(b => b.text() === '查看历史')!.trigger('click'); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.analytics').at(-1)?.[1]).toHaveProperty('modelKey', 'claude-sonnet-4-6');
});

it('keeps exact large counts visible and preserves zero-baseline comparisons', async () => {
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge);
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const response = await original(name, payload);
    if (name === 'stats.analytics' && response.ok) {
      const data = response.data as CommandData<'stats.analytics'>;
      data.totals.totalTokens = '9007199254741115';
      if (data.comparison) data.comparison.changes.tokens = null;
    }
    return response;
  });
  const wrapper = mount(OverviewPage, { props: { initialPreferences: unwrap(await bridge.call('preferences.get', {})) }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  await wrapper.get('[aria-label^="分析周期："]').trigger('click'); await wrapper.get('[data-value="month"][role="radio"]').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('9,007,199,254,741,115');
  expect(wrapper.get('[aria-label="Token 大单位参考"]').text()).toContain('90071992.55 亿');
  expect(wrapper.text()).toContain('上期为零');
});

it('bounds retained remote rows and defers automatic ledger reads for three hours', async () => {
  vi.useFakeTimers();
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge);
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    if (name !== 'wallet.accountEntries') return original(name, payload);
    const page = Number((payload as { cursor?: string }).cursor ?? '0');
    return { ok: true, data: { entries: Array.from({ length: 50 }, (_, index) => ({ id: `${page}/${index}`, kind: 'top_up', occurredAt: '2026-10-18T00:00:00.000Z', R: '1', C: '0', A: '0', note: '充值', originalId: null, intervalId: null, availablePico: '1' })), nextCursor: String(page + 1), revision: 1 } } as never;
  });
  const wrapper = mount(AccountEntries, { props: { vendorId: 'v', mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  expect(wrapper.text()).toContain('远程钱包资金流水'); expect(wrapper.text()).toContain('上次更新');
  for (let i = 0; i < 6; i++) { await wrapper.findAll('button').find(b => b.text() === '加载更早流水')!.trigger('click'); await flushPromises(); }
  expect(wrapper.findAll('tbody tr')).toHaveLength(200);
  const before = call.mock.calls.filter(([name]) => name === 'wallet.accountEntries').length;
  for (let revision = 2; revision <= 10; revision++) bridge.receiveEvent('wallet.changed', { vendorIds: ['v'], revision });
  await flushPromises(); expect(call.mock.calls.filter(([name]) => name === 'wallet.accountEntries')).toHaveLength(before);
  await vi.advanceTimersByTimeAsync(3 * 3600_000); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'wallet.accountEntries')).toHaveLength(before + 1);
  expect(wrapper.findAll('tbody tr')).toHaveLength(50);
});

it('shows a balance source status without timestamps', async () => {
  const bridge = createMockBridge();
  const vendor = unwrap(await bridge.call('wallet.list', {})).vendors[0]!;
  const wrapper = mount(VendorBalanceCard, { props: { vendorId: vendor.id, timezone: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  expect(wrapper.get('.balance-detail').text()).toBe('本地估算');
  expect(wrapper.text()).not.toContain('UTC');
});

