import { flushPromises as flushVue, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type CommandData } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import AccountEntries from '../src/features/wallet/AccountEntries.vue';
import VendorBalanceCard from '../src/features/harness/VendorBalanceCard.vue';

async function flushPromises() { await flushVue(); await vi.dynamicImportSettled(); await flushVue(); }
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });

it('loads all-history statistics and independent model analysis without remote wallet reads', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-18T10:00:00Z'));
  const bridge=createMockBridge(), call=vi.spyOn(bridge,'call');
  const wrapper=mount(OverviewPage,{props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(()=>{wrapper.unmount();bridge.dispose();});await flushPromises();
  expect(call.mock.calls.some(([name])=>name==='wallet.list'||name==='stats.analytics')).toBe(false);
  await wrapper.get('[aria-label^="统计周期："]').trigger('click');await wrapper.get('[data-value="all"][role="radio"]').trigger('click');await flushPromises();
  expect(call.mock.calls.filter(([name])=>name==='stats.explore').at(-1)?.[1]).toMatchObject({rangeKind:'all'});
  expect(wrapper.find('[aria-label="数据统计"]').exists()).toBe(true);
  await wrapper.findAll('[aria-label="统计分析子页"] button').find(b=>b.text()==='数据分析')!.trigger('click');await flushPromises();
  expect(wrapper.get('[aria-label="输出吞吐参考"]').text()).toContain('今日平均tps');
  await wrapper.findAll('button').find(b=>b.text()==='比较模型')!.trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('最多再添加四个模型');
  await wrapper.findAll('button').find(b=>b.text()==='完成选择')!.trigger('click');await flushPromises();
  await wrapper.findAll('[aria-label="分析主题"] button').find(b=>b.text()==='成本')!.trigger('click');await flushPromises();
  expect(wrapper.findAll('button').some(b=>b.text()==='模型详情')).toBe(false);
  expect(wrapper.get('.line-legend').text()).toContain('全部模型总和');
  expect((call.mock.calls.filter(([name])=>name==='stats.explore').at(-1)?.[1] as {modelKeys?:string[]}).modelKeys).toHaveLength(0);
});

it('keeps exact large counts visible in statistics without floating point rounding', async () => {
  const bridge=createMockBridge(),original=bridge.call.bind(bridge);
  vi.spyOn(bridge,'call').mockImplementation(async(name,payload)=>{
    const response=await original(name,payload);
    if(name==='stats.explore'&&response.ok)(response.data as CommandData<'stats.explore'>).totals.totalTokens='9007199254741115';
    return response;
  });
  const wrapper=mount(OverviewPage,{props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))},global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(()=>{wrapper.unmount();bridge.dispose();});await flushPromises();
  expect(wrapper.get('.metrics-grid').text()).toContain('9,007,199,254,741,115');
  expect(wrapper.get('.metrics-grid').text()).toContain('90071992.55 亿');
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

