import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import HourlyDetails from '../src/features/usage-common/HourlyDetails.vue';

const cleanup:(()=>void)[]=[];
afterEach(()=>{cleanup.splice(0).reverse().forEach(stop=>stop());vi.restoreAllMocks();vi.useRealTimers();});

it('retains the table and row DOM while rolling time advances, and cancels work when collapsed',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());
  const range={start:'2026-10-25T00:00:00.000Z',end:'2026-10-25T02:00:00.000Z',offsetMinutes:0,crossesDst:false};
  const original=bridge.call.bind(bridge);let release: (()=>void)|undefined;
  const call=vi.spyOn(bridge,'call').mockImplementation(async(name,payload)=>{
    if(name==='stats.summary' && release!==undefined)await new Promise<void>(resolve=>{release=resolve;});
    return original(name,payload);
  });
  const wrapper=mount(HourlyDetails,{props:{harness:'codex',range,queryKey:'codex/today',models:[],modelValue:null},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(()=>wrapper.unmount());
  await wrapper.get('button').trigger('click');await flushPromises();
  const table=wrapper.get('[aria-label="小时明细表"]'), before=table.text(), row=table.find('tbody tr').element;
  release=()=>{};
  await wrapper.setProps({range:{...range,end:'2026-10-25T02:00:05.000Z'}});await flushPromises();
  expect(table.text()).toBe(before);expect(table.find('tbody tr').element).toBe(row);
  expect(wrapper.text()).not.toContain('当前范围暂无入账用量');
  release();release=undefined;await flushPromises();
  expect(table.find('tbody tr').element).toBe(row);
  release=()=>{};await wrapper.setProps({range:{...range,end:'2026-10-25T02:00:10.000Z'}});await flushPromises();
  await wrapper.get('button').trigger('click');await flushPromises();
  expect(call.mock.calls.some(([name])=>name==='stats.cancelAnalysis')).toBe(true);
  release();release=undefined;await flushPromises();
  expect(wrapper.find('[aria-label="小时明细表"]').exists()).toBe(false);
});
