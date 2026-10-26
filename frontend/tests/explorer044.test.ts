import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type CommandData } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import DimensionExplorer from '../src/features/overview/DimensionExplorer.vue';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import ScopeFilters from '../src/features/overview/ScopeFilters.vue';
import ThroughputReference from '../src/features/overview/ThroughputReference.vue';
import { emptyScope } from '../src/features/overview/scope';
import { defaultAnalysisView } from '../src/features/overview/analysisState';

const cleanups: (() => void)[] = [];
afterEach(() => { cleanups.splice(0).reverse().forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });
const range = {start:'2026-10-20T00:00:00.000Z',end:'2026-10-22T00:00:00.000Z',offsetMinutes:480,crossesDst:false};
const button = (wrapper: VueWrapper, text: string) => wrapper.findAll('button').find(row => row.text() === text)!;
async function fixture() {
  const bridge = createMockBridge(); cleanups.push(() => bridge.dispose());
  const options = unwrap(await bridge.call('stats.exploreOptions', {}));
  return {bridge,options,global:{provide:{[BridgeKey as symbol]:bridge}}};
}

it('submits a comparison once, aggregates a whole supplier, and refreshes the visible series together', async () => {
  const {bridge,options,global} = await fixture(); const call = vi.spyOn(bridge,'call');
  const wrapper = mount(DimensionExplorer,{props:{scope:emptyScope(),range,harness:'all',mode:'UTC',options},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  const reads = () => call.mock.calls.filter(([name]) => name === 'stats.explore');
  expect(reads()).toHaveLength(1);
  await button(wrapper,'比较供应商').trigger('click');
  const choices = wrapper.findAll('.compare-options input');
  await choices[1]!.setValue(true); await choices[2]!.setValue(true); await flushPromises();
  expect(reads()).toHaveLength(1);
  await button(wrapper,'完成选择').trigger('click'); await flushPromises();
  expect(reads()).toHaveLength(2);
  expect(reads().at(-1)![1]).toMatchObject({compareDimension:'vendor',compareIds:['example-a','example-b'],modelKeys:[],sections:expect.arrayContaining(['totals','models','trend','breakdowns','comparison'])});
  expect(wrapper.find('[aria-label="供应商和倍率版本比较趋势"]').exists()).toBe(true);
  await button(wrapper,'更新分析').trigger('click'); await flushPromises();
  expect(reads()).toHaveLength(3);
  expect(reads().at(-1)![1]).toMatchObject({compareIds:['example-a','example-b']});
});

it('preserves empty unknown model keys when entering details and querying throughput', async () => {
  const {bridge,options,global} = await fixture(); const original = bridge.call.bind(bridge);
  const call = vi.spyOn(bridge,'call').mockImplementation(async (name,payload) => {
    const value = await original(name,payload);
    if (name === 'stats.explore' && value.ok) {
      const data = value.data as CommandData<'stats.explore'>;
      const model = data.models[0];
      if (model) data.models = [{...model,modelKey:'',displayName:'未知模型'}];
    }
    return value;
  });
  const wrapper = mount(DimensionExplorer,{props:{scope:emptyScope(),range,harness:'codex',mode:'UTC',options:{...options,models:[{id:'',name:'未知模型',parentId:null}]}},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  await button(wrapper,'模型详情').trigger('click'); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore').at(-1)![1]).toMatchObject({modelKeys:[''],harnesses:[]});
  expect(wrapper.text()).toContain('未知模型 · 模型详情');
  expect(call.mock.calls.filter(([name]) => name === 'stats.outputReferences').at(-1)![1]).toMatchObject({modelKeys:['']});
});

it('remote mode clears unsupported query dimensions and exposes methods without model allocation', async () => {
  const {bridge,options,global} = await fixture(); const call = vi.spyOn(bridge,'call');
  const wrapper = mount(DimensionExplorer,{props:{scope:{harnesses:['codex'],vendorIds:['example-a'],groupIds:['g'],versionIds:['v'],modelKeys:['m']},range,harness:'codex',mode:'UTC',costBasis:'remote',options},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore').at(-1)![1]).toMatchObject({costBasis:'remote',harnesses:[],groupIds:[],versionIds:[],modelKeys:[],vendorIds:['example-a'],sections:['accounts']});
  expect(wrapper.get('[aria-label="远程账户区间"]').text()).toContain('远程累计增量');
  expect(wrapper.get('[aria-label="远程账户独立更正"]').text()).toContain('累计消费更正');
  expect(wrapper.find('[aria-label="筛选模型排名"]').exists()).toBe(false);
});

it('keeps applied version names stable while editing a draft and makes later candidates searchable', async () => {
  const {options,global} = await fixture();
  const vendors = Array.from({length:125},(_,index) => ({id:`vendor-${index}`,name:`供应商 ${index}`,parentId:null}));
  const wrapper = mount(ScopeFilters,{props:{modelValue:{...emptyScope(),groupIds:['example-ga'],versionIds:['example-va1']},options:{...options,vendors,groupVendors:{'example-ga':['vendor-124'],'example-gb':['vendor-124']}}},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  await button(wrapper,'价格组').trigger('click');
  const second = wrapper.findAll('.filter-choice label').find(row => row.text() === '示例价格组 B · 供应商 124')!;
  await second.get('input').setValue(true);
  expect(wrapper.get('p').text()).toContain('0.9 倍率');
  await button(wrapper,'供应商').trigger('click');
  await wrapper.get('[aria-label="搜索供应商价格组版本"]').setValue('供应商 124'); await flushPromises();
  await wrapper.get('.filter-choice input').setValue(true);
  await button(wrapper,'应用筛选').trigger('click');
  expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toMatchObject({vendorIds:['vendor-124'],versionIds:['example-va1']});
});

it('saves applied and draft state separately and avoids model-directory scans on balance events', async () => {
  vi.useFakeTimers();
  const {bridge,global} = await fixture(); const initialPreferences = unwrap(await bridge.call('preferences.get',{})); const call = vi.spyOn(bridge,'call');
  const wrapper = mount(OverviewPage,{props:{initialPreferences},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  const supplier = wrapper.findAll('.scope-filters label').find(row => row.text().startsWith('示例价格组 A · '))!;
  await supplier.get('input').setValue(true); await button(wrapper,'应用筛选').trigger('click'); await flushPromises();
  await wrapper.findAll('.scope-filters label').find(row => row.text().startsWith('示例价格组 B · '))!.get('input').setValue(true);
  await vi.advanceTimersByTimeAsync(350); await flushPromises();
  const state = unwrap(await bridge.call('preferences.get',{})).preferences.statisticsView!;
  expect(state.scope.groupIds).toEqual(['example-ga']); expect(state.draft.groupIds).toEqual(['example-ga','example-gb']);
  const before = call.mock.calls.filter(([name]) => name === 'stats.exploreOptions').length;
  bridge.receiveEvent('wallet.changed',{vendorIds:['example-ga'],revision:3}); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.exploreOptions')).toHaveLength(before);
  bridge.receiveEvent('wallet.changed',{vendorIds:['example-ga'],revision:4,catalogChanged:true}); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.exploreOptions')).toHaveLength(before+1);
  expect(defaultAnalysisView().focus).toBeNull();
});

it('does not query throughput again for each draft model checkbox', async () => {
  const {bridge,options,global} = await fixture(); const call = vi.spyOn(bridge,'call');
  const wrapper = mount(ThroughputReference,{props:{scope:emptyScope(),options},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  const before = call.mock.calls.filter(([name]) => name === 'stats.outputReferences').length;
  await button(wrapper,'比较模型').trigger('click'); await flushPromises();
  const inputs = wrapper.findAll('.comparison-models input');
  await inputs[1]!.setValue(true); await inputs[2]!.setValue(true); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.outputReferences')).toHaveLength(before);
  await button(wrapper,'完成选择').trigger('click'); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.outputReferences')).toHaveLength(before+1);
});

it('disables unrelated group/version choices and restores selected names outside the first model page', async () => {
  const {options,global} = await fixture();
  const wrapper = mount(ScopeFilters,{props:{modelValue:{...emptyScope(),vendorIds:['example-a'],modelKeys:['late-model']},options:{...options,selectedModels:[{id:'late-model',name:'末页模型',parentId:null}]}},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.get('p').text()).toContain('末页模型');
  await button(wrapper,'价格组').trigger('click');
  const unrelated = wrapper.findAll('.filter-choice').find(row => row.text().includes('示例价格组 B'))!;
  expect((unrelated.get('input').element as HTMLInputElement).disabled).toBe(true);
  expect((unrelated.get('button').element as HTMLButtonElement).disabled).toBe(true);
  expect((wrapper.findAll('.filter-choice').find(row => row.text().includes('示例价格组 A'))!.get('input').element as HTMLInputElement).disabled).toBe(false);
});

it('cancels primary throughput model changes with Escape and keeps the applied curve', async () => {
  const {bridge,options,global} = await fixture(); const call = vi.spyOn(bridge,'call');
  const wrapper = mount(ThroughputReference,{attachTo:document.body,props:{scope:emptyScope(),options},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  const before = call.mock.calls.filter(([name]) => name === 'stats.outputReferences').length;
  await button(wrapper,'比较模型').trigger('click'); await flushPromises();
  await wrapper.findAll('.comparison-models button')[0]!.trigger('click');
  await wrapper.get('section').trigger('keydown',{key:'Escape'}); await flushPromises();
  const reads = call.mock.calls.filter(([name]) => name === 'stats.outputReferences');
  expect(reads.at(-1)![1]).toMatchObject({modelKeys:['gpt-6']});
  expect(reads.length).toBeLessThanOrEqual(before+1);
  expect(document.activeElement?.textContent).toBe('比较模型');
});

it('returns keyboard focus from the comparison picker without applying its draft', async () => {
  const {bridge,options,global} = await fixture(); const call = vi.spyOn(bridge,'call');
  const wrapper = mount(DimensionExplorer,{attachTo:document.body,props:{scope:emptyScope(),range,harness:'all',mode:'UTC',options},global}); cleanups.push(() => wrapper.unmount()); await flushPromises();
  await button(wrapper,'比较供应商').trigger('click');
  await wrapper.get('.compare-options input').setValue(true);
  await wrapper.get('section').trigger('keydown',{key:'Escape'}); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore')).toHaveLength(1);
  expect(document.activeElement?.textContent).toBe('比较供应商');
  expect(wrapper.find('.compare-picker').exists()).toBe(false);
});
