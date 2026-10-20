import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import { unwrap } from '../src/app/errors';
import { useCommand } from '../src/shared/command';
import AccountAdjustmentDialog from '../src/features/wallet/AccountAdjustmentDialog.vue';
import VendorCard from '../src/features/wallet/VendorCard.vue';
import UnpricedNotice from '../src/features/usage-common/UnpricedNotice.vue';

const cleanups: (() => void)[]=[];
afterEach(()=>{cleanups.splice(0).forEach(fn=>fn());vi.restoreAllMocks();document.body.innerHTML='';});

it('uses account totals while preserving local estimate information and correction entry points',async()=>{
  const bridge=createMockBridge();cleanups.push(()=>bridge.dispose());
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  vendor.account={R:'5000000000000',C:'2260000000000',A:'0',B:'4000000000000',localEstimatePico:'2130000000000',observedAt:'2026-10-18T10:00:00.000Z',coverageStart:'2026-10-01T00:00:00.000Z',methods:['balance_estimate'],remote:true,revision:4};
  const wrapper=mount(VendorCard,{props:{vendor,mode:'UTC'}});cleanups.push(()=>wrapper.unmount());
  expect(wrapper.get('.balance').text()).toContain('$4.0000');
  expect(wrapper.text()).toContain('$2.2600');expect(wrapper.text()).toContain('$2.1300');
  await wrapper.findAll('.amount-link')[0]!.trigger('click');
  expect(wrapper.emitted('correct')?.[0]).toEqual(['top_up']);
});

it('reclassifies six-decimal amounts and leaves sub-micro remainder untouched',async()=>{
  const bridge=createMockBridge();cleanups.push(()=>bridge.dispose());const original=bridge.call.bind(bridge);
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  const spy=vi.spyOn(bridge,'call').mockImplementation(async(command,payload)=>{
    if(command==='wallet.accountEntries')return {ok:true,data:{entries:[{id:'cal',kind:'adjustment',occurredAt:'2026-10-18T00:00:00.000Z',R:'0',C:'0',A:'1234567890123',note:'校准',originalId:null,intervalId:null,availablePico:'1234567890123'}],nextCursor:null,revision:4}} as never;
    if(command==='wallet.reclassify')return {ok:true,data:{vendor,vendorId:vendor.id,action:null,changed:true,revision:2,pricingRevision:1}} as never;
    return original(command,payload);
  });
  const wrapper=mount(AccountAdjustmentDialog,{props:{vendor,target:'top_up'},attachTo:document.body,global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});cleanups.push(()=>wrapper.unmount());await flushPromises();
  expect(spy.mock.calls.some(([name])=>name==='wallet.accountEntries')).toBe(false);
  await wrapper.get('[aria-label="选择原流水"]').trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('流水发生时间');
  expect(wrapper.text()).toContain('调整与校准');
  await wrapper.get('[aria-label="选择流水 cal"]').setValue();
  await wrapper.findAll('button').find(button=>button.text()==='使用此流水')!.trigger('click');await flushPromises();
  expect((wrapper.get('[aria-label="校准金额"]').element as HTMLInputElement).value).toBe('1.234567');
  await wrapper.get('form').trigger('submit');await flushPromises();
  expect(spy.mock.calls.find(([name])=>name==='wallet.reclassify')?.[1]).toMatchObject({amountPico:'1234567000000',targetKind:'top_up',expectedAccountRevision:4});
});

it('closing an unpriced notice only changes its display preference and can be reversed',async()=>{
  const bridge=createMockBridge();cleanups.push(()=>bridge.dispose());const original=bridge.call.bind(bridge);
  const spy=vi.spyOn(bridge,'call').mockImplementation(async(command,payload)=>command==='pricing.unpricedNotice'?{ok:true,data:{count:2,harnesses:['codex'],signature:'7:2'}} as never:original(command,payload));
  const wrapper=mount(UnpricedNotice,{props:{harness:'codex',initialPreferences:unwrap(await bridge.call('preferences.get',{}))},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanups.push(()=>wrapper.unmount());await flushPromises();
  await wrapper.get('button').trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('恢复未定价提醒');
  expect(spy.mock.calls.some(([name])=>name==='pricing.resolveUnpriced')).toBe(false);
  await wrapper.get('button').trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('前往未定价模型补价');
});

it('retains an operation key when a bounded write reports partial completion',async()=>{
  const bridge=createMockBridge();cleanups.push(()=>bridge.dispose());
  const ids:string[]=[];
  vi.spyOn(bridge,'call').mockImplementation(async(_name,payload)=>{ids.push((payload as {operationId:string}).operationId);return {ok:true,data:{recalculated:500,remaining:1,costDeltaPico:'1',revision:3,complete:ids.length>1,errorCode:ids.length===1?'WRITE_TIMEOUT':null}} as never;});
  const command=useCommand('pricing.recalculateUnpriced',{bridge});
  await command.run({expectedPricingRevision:1});await command.run({expectedPricingRevision:1});
  expect(ids[0]).toBe(ids[1]);
});
