import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import AccountEntries from '../src/features/wallet/AccountEntries.vue';
import FlowCorrectionDialog from '../src/features/wallet/FlowCorrectionDialog.vue';
import WalletPage from '../src/features/wallet/WalletPage.vue';
import LocalWalletDialog from '../src/features/wallet/LocalWalletDialog.vue';
import VendorCard from '../src/features/wallet/VendorCard.vue';

const cleanup:(()=>void)[]=[];
afterEach(()=>{cleanup.splice(0).forEach(fn=>fn());vi.restoreAllMocks();document.body.innerHTML='';});

it.each(['local','remote'] as const)('shows pending recharge and consumption separately in the %s wallet',async(scope)=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  const totals={R:'5000000000000',C:'3000000000000',A:'0',B:'2000000000000',pastTopUpPico:'0',pastConsumptionPico:'0',postedR:'4000000000000',postedC:'1000000000000',pendingTopUpPico:'1000000000000',pendingConsumptionPico:'2000000000000'};
  vendor.localWallet=totals;vendor.remoteWallet=totals;
  const wrapper=mount(VendorCard,{props:{vendor,scope,mode:'UTC'}});cleanup.push(()=>wrapper.unmount());
  expect(wrapper.get('[aria-label="待更新充值金额"]').text()).toBe('$1.0000');
  expect(wrapper.get('[aria-label="待更新消费金额"]').text()).toBe('$2.0000');
  await wrapper.setProps({vendor:{...vendor,[scope==='local'?'localWallet':'remoteWallet']:{...totals,postedR:totals.R,postedC:totals.C,pendingTopUpPico:'0',pendingConsumptionPico:'0'}}});
  expect(wrapper.get('[aria-label="待更新充值金额"]').text()).toBe('$0.0000');
  expect(wrapper.get('[aria-label="待更新消费金额"]').text()).toBe('$0.0000');
});

it('closes only the selected notice and refreshes without altering the flow amount',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());const original=bridge.call.bind(bridge);
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  const entry={id:'f:notice',kind:'consumption',occurredAt:'2026-10-19T00:00:00.000Z',R:'0',C:'2000000000000',A:'0',note:'已校准',originalId:null,intervalId:null,availablePico:'0',entryRevision:2,manuallyCalibrated:true,estimatedConsumptionPico:'1000000000000',noticeId:'notice:1' as string|null};
  const spy=vi.spyOn(bridge,'call').mockImplementation(async(name,payload)=>{
    if(name==='wallet.accountEntries')return {ok:true,data:{entries:[{...entry}],nextCursor:null,revision:2}} as never;
    if(name==='wallet.dismissNotice'){entry.noticeId=null;return {ok:true,data:{vendor,vendorId:vendor.id,action:null,changed:true,revision:3,pricingRevision:1}} as never;}
    return original(name,payload);
  });
  const wrapper=mount(AccountEntries,{props:{vendorId:vendor.id,scope:'local',mode:'UTC'},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(()=>wrapper.unmount());await flushPromises();
  expect(wrapper.text()).toContain('估算与记账仍有差额');
  await wrapper.get('[aria-label="关闭估算核对提醒"]').trigger('click');await flushPromises();
  expect(spy.mock.calls.find(([name])=>name==='wallet.dismissNotice')?.[1]).toMatchObject({vendorId:vendor.id,entryId:'f:notice',noticeId:'notice:1'});
  expect(wrapper.find('.estimate-notice').exists()).toBe(false);
  expect(wrapper.text()).toContain('已人工校准');expect(entry.C).toBe('2000000000000');
  expect(spy.mock.calls.some(([name])=>name==='wallet.correctEntry'||name==='wallet.setLocalTotals')).toBe(false);
});

it('starts with all flow types and sends independent date/type filters to the selected book',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());const spy=vi.spyOn(bridge,'call');
  const wrapper=mount(AccountEntries,{props:{vendorId:'v',scope:'local',mode:'UTC'},global:{provide:{[BridgeKey as symbol]:bridge}}});cleanup.push(()=>wrapper.unmount());await flushPromises();
  expect(spy.mock.calls.filter(([name])=>name==='wallet.accountEntries').at(-1)?.[1]).toMatchObject({kind:'all',scope:'local',startDay:null,endDay:null});
  expect(wrapper.get('select[aria-label="流水类型"]').text()).toContain('调整与校准');
  await wrapper.get('[aria-label="流水类型"]').setValue('consumption');await flushPromises();
  await wrapper.get('[aria-label="流水开始日期"]').setValue('2026-10-01');await flushPromises();
  await wrapper.get('[aria-label="流水结束日期"]').setValue('2026-10-19');await flushPromises();
  expect(spy.mock.calls.filter(([name])=>name==='wallet.accountEntries').at(-1)?.[1]).toMatchObject({kind:'consumption',startDay:'2026-10-01',endDay:'2026-10-19'});
});

it('a note-only correction preserves sub-micro precision and binds the source revision',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());const original=bridge.call.bind(bridge);
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  const spy=vi.spyOn(bridge,'call').mockImplementation(async(name,payload)=>name==='wallet.correctEntry'?{ok:true,data:{vendor,vendorId:vendor.id,action:null,changed:true,revision:3,pricingRevision:1}} as never:original(name,payload));
  const entry={id:'f:1',kind:'consumption' as const,occurredAt:'2026-10-19T00:00:00.000Z',R:'0',C:'1234567890123',A:'0',note:'原备注',originalId:null,intervalId:null,availablePico:'0',entryRevision:7,beforeR:null,beforeC:null,beforeB:null,afterR:null,afterC:null,afterB:null,fromAt:null,toAt:null,postedAt:null,manuallyCalibrated:false,estimatedConsumptionPico:null,noticeId:null};
  const wrapper=mount(FlowCorrectionDialog,{props:{vendorId:vendor.id,scope:'remote',entry},global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});cleanup.push(()=>wrapper.unmount());
  await wrapper.get('[aria-label="流水校准备注"]').setValue('只改备注');
  await wrapper.get('form').trigger('submit');await flushPromises();
  expect(spy.mock.calls.find(([name])=>name==='wallet.correctEntry')?.[1]).toMatchObject({scope:'remote',C:'1234567890123',R:'0',expectedEntryRevision:7,note:'只改备注'});
});

it('saving past amounts does not round untouched local consumption or recharge',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());const original=bridge.call.bind(bridge);
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  const spy=vi.spyOn(bridge,'call').mockImplementation(async(name,payload)=>name==='wallet.setLocalTotals'?{ok:true,data:{vendor,vendorId:vendor.id,action:null,changed:true,revision:3,pricingRevision:1}} as never:original(name,payload));
  vendor.localWallet={R:'1000000000001',C:'1234567890123',B:'0',A:'0',pastTopUpPico:'0',pastConsumptionPico:'0',postedR:null,postedC:null,pendingTopUpPico:'0',pendingConsumptionPico:'0'};
  const wrapper=mount(LocalWalletDialog,{props:{vendor},global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});cleanup.push(()=>wrapper.unmount());
  await wrapper.get('[aria-label="本地过往充值"]').setValue('3');await wrapper.get('form').trigger('submit');await flushPromises();
  const payload=spy.mock.calls.find(([name])=>name==='wallet.setLocalTotals')![1];
  expect(payload).toMatchObject({pastTopUpPico:'3000000000000'});expect(payload).not.toHaveProperty('R');expect(payload).not.toHaveProperty('C');
});

it('wallet visibility is saved per vendor, overrides an invisible default and can be restored',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());
  const create=async()=>mount(WalletPage,{props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))},global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});
  const wrapper=await create();await flushPromises();
  await wrapper.get('[aria-label="显示钱包"]').setValue('remote');await flushPromises();
  expect(wrapper.findAll('[aria-label="钱包分类"] button').map(button=>button.text())).toEqual(['远程钱包']);
  const selected=wrapper.get('.vendor-heading h2').text();wrapper.unmount();
  const restored=await create();cleanup.push(()=>restored.unmount());await flushPromises();
  expect(restored.get('.vendor-heading h2').text()).toBe(selected);
  expect(restored.get('[aria-label="显示钱包"]').element).toHaveProperty('value','remote');
  expect(restored.get('[aria-label="钱包分类"] [aria-current="page"]').text()).toBe('远程钱包');
  await restored.get('[aria-label="显示钱包"]').setValue('both');await flushPromises();
  expect(restored.findAll('[aria-label="钱包分类"] button')).toHaveLength(2);
});
