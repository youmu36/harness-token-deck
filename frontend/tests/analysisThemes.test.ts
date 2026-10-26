import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { reactive } from 'vue';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import AnalysisTheme from '../src/features/overview/AnalysisTheme.vue';
import UsageComposition from '../src/features/overview/UsageComposition.vue';
import { defaultAnalysisView } from '../src/features/overview/analysisState';
import { millionTokens } from '../src/features/overview/valueFormat';
import { emptyScope } from '../src/features/overview/scope';
import { priceGroupOptions, priceVersionOptions } from '../src/features/overview/comparisonOptions';
const cleanup:(() => void)[] = [];
afterEach(() => {cleanup.splice(0).reverse().forEach(fn => fn());vi.useRealTimers();vi.restoreAllMocks();});
const button = (wrapper:VueWrapper,name:string) => wrapper.findAll('button').find(row => row.text() === name)!;
const range = {start:'2026-10-01T00:00:00.000Z',end:'2026-10-20T00:00:00.000Z',offsetMinutes:0,crossesDst:false};
it('opens analysis directly with four themes and reads only the visible theme',async () => {
  vi.useFakeTimers();vi.setSystemTime(new Date('2026-10-25T08:00:00Z'));
  const bridge = createMockBridge(), call = vi.spyOn(bridge,'call');cleanup.push(() => bridge.dispose());
  const initialPreferences = unwrap(await bridge.call('preferences.get',{}));
  initialPreferences.preferences.analysisView = {...defaultAnalysisView(),section:'analysis',period:'month'};
  const wrapper = mount(OverviewPage,{props:{initialPreferences},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(() => wrapper.unmount());
  await vi.dynamicImportSettled();await flushPromises();
  expect(wrapper.get('[aria-label="分析主题"]').findAll('button').map(row => row.text())).toEqual(['TPS','成本','缓存','性价比']);
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore')).toHaveLength(0);
  expect(call.mock.calls.some(([name]) => name === 'stats.outputReferences')).toBe(true);
  await button(wrapper,'成本').trigger('click');await vi.dynamicImportSettled();await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore').at(-1)?.[1]).toMatchObject({costBasis:'local',sections:expect.arrayContaining(['models','totals','trend'])});
  const before = call.mock.calls.filter(([name]) => name === 'stats.explore').length;
  bridge.receiveEvent('output.changed',{revision:3});await flushPromises();expect(call.mock.calls.filter(([name]) => name === 'stats.explore')).toHaveLength(before);
  await button(wrapper,'性价比').trigger('click');await vi.dynamicImportSettled();await flushPromises();
  expect(wrapper.text()).toContain('M Token/USD');expect(wrapper.find('[aria-label="输出吞吐参考"]').exists()).toBe(false);
});
it('applies model comparison once and preserves focus when cancelling its draft',async () => {
  vi.useFakeTimers();vi.setSystemTime(new Date('2026-10-25T08:00:00Z'));
  const bridge = createMockBridge(), call = vi.spyOn(bridge,'call');cleanup.push(() => bridge.dispose());
  const options = unwrap(await bridge.call('stats.exploreOptions',{})), view = reactive(defaultAnalysisView());
  const wrapper = mount(AnalysisTheme,{props:{theme:'value',scope:emptyScope(),range,rangeKind:'month',resolveRange:() => range,view,options},attachTo:document.body,global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(() => wrapper.unmount());await flushPromises();
  const reads = () => call.mock.calls.filter(([name]) => name === 'stats.explore');const before = reads().length;
  await button(wrapper,'比较模型或价格组').trigger('click');await flushPromises();
  const choices = wrapper.findAll('.candidate-list input');await choices[0]!.setValue(true);await choices[1]!.setValue(true);await flushPromises();
  expect(reads()).toHaveLength(before);
  await button(wrapper,'完成选择').trigger('click');await flushPromises();expect(reads()).toHaveLength(before+1);
  expect(reads().at(-1)?.[1]).toMatchObject({compareDimension:'model',compareIds:expect.arrayContaining(['gpt-6','deepseek-v3.2'])});
  await button(wrapper,'比较模型或价格组').trigger('click');await wrapper.get('.comparison-picker').trigger('keydown',{key:'Escape'});await flushPromises();
  expect(document.activeElement).toBe(wrapper.get('[data-compare]').element);expect(reads()).toHaveLength(before+1);
});
it('formats million units exactly and marks tiny positive values without changing their share',async () => {
  expect(millionTokens('1234567.8')).toBe('1.23');expect(millionTokens('1005000.0')).toBe('1.01');
  expect(millionTokens('0.1')).toBe('0.00（非零）');expect(millionTokens(null)).toBe('—');
  const bridge = createMockBridge();cleanup.push(() => bridge.dispose());
  const snapshot = unwrap(await bridge.call('stats.explore',{start:range.start,end:range.end}));
  snapshot.totals.eligibleTokens = '1000000000';snapshot.totals.parts.output = '1';
  const wrapper = mount(UsageComposition,{props:{snapshot}});cleanup.push(() => wrapper.unmount());
  expect(wrapper.text()).toContain('0.00%（非零）');
  expect(wrapper.findAll('.token-composition li').find(row => row.text().includes('输出'))?.text()).toContain('1 Token');
});

it('keeps model structure and comparison on the main page without extra analysis reads',async () => {
  const bridge=createMockBridge(),call=vi.spyOn(bridge,'call');cleanup.push(() => bridge.dispose());
  const options=unwrap(await bridge.call('stats.exploreOptions',{})),view=reactive({...defaultAnalysisView(),focus:'gpt-6'});
  const wrapper=mount(AnalysisTheme,{props:{theme:'cache',scope:emptyScope(),range,rangeKind:'month',resolveRange:() => range,view,options},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(() => wrapper.unmount());await flushPromises();
  const reads=() => call.mock.calls.filter(([name]) => name==='stats.explore');
  expect(reads().at(-1)?.[1]).toMatchObject({modelKeys:[]});
  expect(wrapper.text()).not.toContain('模型详情');
  expect(wrapper.get('.line-legend').text()).toContain('全部模型总和');
  expect(wrapper.get('.token-composition h3').text()).toContain('全部模型总和');
  const before=reads().length;
  await wrapper.get('[aria-label^="Token 使用结构视图"]').trigger('click');
  await wrapper.findAll('[role="radio"]')[1]!.trigger('click');await flushPromises();
  expect(wrapper.get('.token-composition h3').text()).not.toContain('全部模型总和');expect(reads()).toHaveLength(before);
  await button(wrapper,'比较模型或价格组').trigger('click');await flushPromises();
  await wrapper.findAll('.candidate-list input')[0]!.setValue(true);await button(wrapper,'完成选择').trigger('click');await flushPromises();
  expect(reads()).toHaveLength(before+1);expect(wrapper.get('.line-legend').text()).not.toContain('全部模型总和');
  await button(wrapper,'清除比较').trigger('click');await flushPromises();
  expect(reads().at(-1)?.[1]).toMatchObject({compareIds:[]});expect(wrapper.get('.line-legend').text()).toContain('全部模型总和');
});

it('shows per-model value components inline and keeps units outside the numeric axis',async () => {
  const bridge=createMockBridge();cleanup.push(() => bridge.dispose());const view=reactive(defaultAnalysisView());
  const wrapper=mount(AnalysisTheme,{props:{theme:'value',scope:emptyScope(),range,rangeKind:'month',resolveRange:() => range,view},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(() => wrapper.unmount());await flushPromises();
  expect(wrapper.findAll('.model-ranking tbody tr')[0]!.findAll('td')).toHaveLength(7);
  expect(wrapper.get('.value-parts h3').text()).toContain('全部模型总和');
  expect(wrapper.get('.line-unit').text()).toBe('单位：Token/USD');
  expect(wrapper.findAll('.theme-chart .axis text').slice(0,5).every(row => row.text().endsWith(' M'))).toBe(true);
});

it('qualifies same-named price groups and versions with their linked suppliers',() => {
  const options={vendors:[{id:'a',name:'供应商甲',parentId:null},{id:'b',name:'供应商乙',parentId:null}],groups:[{id:'ga',name:'一倍率',parentId:null},{id:'gb',name:'一倍率',parentId:null}],versions:[{id:'va',name:'默认',parentId:'ga'}],groupVendors:{ga:['a'],gb:['b']},models:[],selectedModels:[],totalModels:0,modelNextOffset:null};
  expect(priceGroupOptions(options).map(row => row.name)).toEqual(['一倍率 · 供应商甲','一倍率 · 供应商乙']);
  expect(priceVersionOptions(options)[0]!.name).toBe('默认 · 一倍率 · 供应商甲');
});
