import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, expect, it, vi, type MockInstance } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });
const button = (wrapper:VueWrapper,text:string) => wrapper.findAll('button').find(row => row.text() === text)!;
const reads = (call:MockInstance<Bridge['call']>) => call.mock.calls.filter(([name]) => name === 'stats.explore');
async function render() {
  const bridge = createMockBridge(), call = vi.spyOn(bridge,'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get',{}));
  const wrapper = mount(OverviewPage,{props:{initialPreferences},attachTo:document.body,global:{provide:{[BridgeKey as symbol]:bridge}}});
  cleanup.push(() => {wrapper.unmount();bridge.dispose();});
  await vi.dynamicImportSettled(); await flushPromises(); return {bridge,call,wrapper};
}
it('renders six exact metrics, stacked usage, request counts and two navigation pages',async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const {call,wrapper} = await render();
  expect(reads(call)).toHaveLength(1); expect(reads(call)[0]![1]).toMatchObject({harnesses:[],sections:expect.arrayContaining(['totals','models','trend'])});
  expect(wrapper.findAll('.metrics-grid .metric')).toHaveLength(6);
  expect(wrapper.get('[aria-label="统计分析子页"]').findAll('button').map(row => row.text())).toEqual(['数据统计','数据分析']);
  expect(wrapper.find('[aria-label^="用量历史柱状图"]').exists()).toBe(true);
  expect(wrapper.get('[aria-label="模型明细表"]').text()).toContain('请求数');
  expect(wrapper.get('[aria-label="按来源汇总表"]').findAll('tbody tr').length).toBeGreaterThan(0);
  expect(wrapper.find('[aria-label^="费用口径"]').exists()).toBe(false);
  expect(wrapper.findAll('.scope-filters summary').map(row => row.text())).toEqual(['来源全部','价格组全部']);
});
it('advances event reads and refreshes once after hidden changes',async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const {bridge,call} = await render();
  vi.setSystemTime(new Date('2026-10-06T08:05:00Z')); bridge.receiveEvent('usage.changed',{harnesses:['dsh'],revision:2}); await flushPromises();
  expect(reads(call)).toHaveLength(2); expect(reads(call).at(-1)?.[1]).toMatchObject({end:'2026-10-06T08:05:00.000Z'});
  bridge.receiveEvent('app.visibility',{visible:false});
  for(let revision=3;revision<8;revision++) bridge.receiveEvent('usage.changed',{harnesses:['zcode'],revision});
  await flushPromises(); expect(reads(call)).toHaveLength(2);
  vi.setSystemTime(new Date('2026-10-06T08:10:00Z')); bridge.receiveEvent('app.visibility',{visible:true}); await flushPromises();
  expect(reads(call)).toHaveLength(3);
});
it('saves statistics time independently without the removed collection header',async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const {bridge,call,wrapper} = await render();
  await wrapper.get('[aria-label^="时间范围："]').trigger('click'); await wrapper.get('[role="radio"][data-value="7days"]').trigger('click');
  await vi.advanceTimersByTimeAsync(350); await flushPromises();
  const prefs = unwrap(await bridge.call('preferences.get',{})).preferences;
  expect(prefs.statisticsView?.recent.range).toBe('7days'); expect(prefs.analysisView).toBeNull(); expect(prefs.filters.codex).toBeUndefined();
  expect(reads(call).length).toBeGreaterThan(1);
  expect(wrapper.find('.scan-control').exists()).toBe(false);
  expect(call.mock.calls.some(([name]) => name === 'collection.status')).toBe(false);
});
it('filters by source and ignores unrelated usage and wallet balance events',async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const {bridge,call,wrapper} = await render();
  await wrapper.findAll('.scope-filters label').find(row => row.text() === 'Codex')!.get('input').setValue(true);
  await button(wrapper,'应用筛选').trigger('click'); await flushPromises(); expect(reads(call).at(-1)?.[1]).toMatchObject({harnesses:['codex']});
  const count = reads(call).length;
  bridge.receiveEvent('wallet.changed',{vendorIds:['example-a'],revision:2}); bridge.receiveEvent('usage.changed',{harnesses:['dsh'],revision:2});
  await vi.advanceTimersByTimeAsync(5000); await flushPromises(); expect(reads(call)).toHaveLength(count);
  bridge.receiveEvent('cost.changed',{revision:1}); await flushPromises(); expect(reads(call)).toHaveLength(count+1);
});
it('loads hourly detail only when expanded and preserves price-group filters',async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T08:00:00Z'));
  const {call,wrapper} = await render();
  await wrapper.findAll('.scope-filters label').find(row => row.text().startsWith('示例价格组 A · '))!.get('input').setValue(true);
  await button(wrapper,'应用筛选').trigger('click'); await flushPromises();
  expect(reads(call).every(([,request]) => (request as {grain:string}).grain !== 'hour')).toBe(true);
  await button(wrapper,'查看小时明细').trigger('click'); await flushPromises();
  expect(reads(call).at(-1)?.[1]).toMatchObject({groupIds:['example-ga'],grain:'hour',sections:['trend']});
  expect(wrapper.find('[data-value="cost"]').exists()).toBe(false);
});
