import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import WalletPage from '../src/features/wallet/WalletPage.vue';
import LocalWalletDialog from '../src/features/wallet/LocalWalletDialog.vue';
import VendorCard from '../src/features/wallet/VendorCard.vue';

const cleanup:(()=>void)[]=[];
afterEach(()=>{cleanup.splice(0).forEach(fn=>fn());vi.restoreAllMocks();document.body.innerHTML='';});

it('separates local and remote totals, displays six cards in recharge/consumption order',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  vendor.localWallet={R:'1000000000000',C:'2000000000000',B:'-1000000000000',A:'0',pastTopUpPico:'3000000000000',pastConsumptionPico:'4000000000000',postedR:null,postedC:null,pendingUpdatedAt: null, pendingTopUpPico:'0',pendingConsumptionPico:'0'};
  vendor.remoteWallet={...vendor.localWallet,R:'9000000000000',C:'8000000000000',B:'1000000000000'};
  const wrapper=mount(VendorCard,{props:{vendor,mode:'UTC',scope:'local'}});cleanup.push(()=>wrapper.unmount());
  expect(wrapper.findAll('dt').map(row=>row.text())).toEqual(['累计充值待更新金额','过往充值','充值总额','累计消费待更新金额','过往消费','消费总额']);
  expect(wrapper.findAll('dd')[2]!.text()).toBe('$4.0000');
  expect(wrapper.findAll('dd')[5]!.text()).toBe('$6.0000');
  expect(wrapper.get('.balance').text()).toContain('-$1.0000');
  await wrapper.setProps({scope:'remote'});
  expect(wrapper.findAll('dd')[0]!.get('.money').text()).toBe('$9.0000');
});

it('reads only the selected wallet book and keeps monthly accounting lazy',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());const call=vi.spyOn(bridge,'call');
  const wrapper=mount(WalletPage,{props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))},global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});cleanup.push(()=>wrapper.unmount());await flushPromises();
  expect(call.mock.calls.some(([name])=>name==='wallet.accountEntries'||name==='wallet.dailyBook')).toBe(false);
  const click=async(label:string,scope:string)=>{await wrapper.findAll(`[aria-label="${scope}"] button`).find(row=>row.text()===label)!.trigger('click');await flushPromises();};
  await click('资金流水','供应商二级导航');
  expect(call.mock.calls.filter(([name])=>name==='wallet.accountEntries').at(-1)?.[1]).toMatchObject({scope:'local'});
  await click('远程钱包','钱包分类');
  expect(call.mock.calls.filter(([name])=>name==='wallet.accountEntries').at(-1)?.[1]).toMatchObject({scope:'remote'});
  await click('记账本','供应商二级导航');
  expect(call.mock.calls.filter(([name])=>name==='wallet.dailyBook').at(-1)?.[1]).toMatchObject({scope:'remote'});
  expect(wrapper.get('[aria-label="钱包记账本"]').text()).toContain('本月消费');
});

it('submits exact local totals and never calls a remote correction command',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());const original=bridge.call.bind(bridge);
  const vendor=unwrap(await bridge.call('wallet.list',{})).vendors[0]!;
  const call=vi.spyOn(bridge,'call').mockImplementation(async(name,payload)=>name==='wallet.setLocalTotals'?{ok:true,data:{vendor,vendorId:vendor.id,action:null,changed:true,revision:3,pricingRevision:1}} as never:original(name,payload));
  vendor.localWallet={R:'1000000000123',C:'1234567890123',B:'0',A:'0',pastTopUpPico:'0',pastConsumptionPico:'0',postedR:null,postedC:null,pendingUpdatedAt: null, pendingTopUpPico:'0',pendingConsumptionPico:'0'};
  const wrapper=mount(LocalWalletDialog,{props:{vendor},attachTo:document.body,global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});cleanup.push(()=>wrapper.unmount());await flushPromises();
  expect(wrapper.find('[aria-label="本地累计充值"]').exists()).toBe(false);
  await wrapper.get('[aria-label="本地累计消费"]').setValue('1.1234567');
  await wrapper.get('form').trigger('submit');await flushPromises();
  expect(call.mock.calls.some(([name])=>name==='wallet.setLocalTotals')).toBe(false);
  await wrapper.get('[aria-label="本地累计消费"]').setValue('1.123456');
  await wrapper.get('form').trigger('submit');await flushPromises();
  expect(call.mock.calls.find(([name])=>name==='wallet.setLocalTotals')?.[1]).toMatchObject({C:'1123456000000',expectedWalletRevision:vendor.walletRevision});
  expect(call.mock.calls.find(([name])=>name==='wallet.setLocalTotals')?.[1]).not.toHaveProperty('R');
  expect(call.mock.calls.some(([name])=>name==='wallet.correctConsumption'||name==='wallet.reclassify')).toBe(false);
  expect(wrapper.emitted('saved')).toHaveLength(1);
});

it('saves either wallet as the default and restores it when reopening or selecting a vendor',async()=>{
  const bridge=createMockBridge();cleanup.push(()=>bridge.dispose());
  const create=async()=>mount(WalletPage,{props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))},global:{provide:{[BridgeKey as symbol]:bridge},stubs:{teleport:true}}});
  const wrapper=await create();await flushPromises();
  await wrapper.findAll('[aria-label="钱包分类"] button').find(row=>row.text()==='远程钱包')!.trigger('click');
  await wrapper.findAll('button').find(row=>row.text()==='设为默认钱包')!.trigger('click');await flushPromises();
  expect(unwrap(await bridge.call('preferences.get',{})).preferences.walletDefaultScope).toBe('remote');
  await wrapper.findAll('[aria-label="钱包分类"] button').find(row=>row.text()==='本地钱包')!.trigger('click');
  await wrapper.get('[aria-label="供应商列表"] button').trigger('click');await flushPromises();
  expect(wrapper.get('[aria-label="钱包分类"] [aria-current="page"]').text()).toBe('远程钱包');
  wrapper.unmount();
  const restored=await create();cleanup.push(()=>restored.unmount());await flushPromises();
  expect(restored.get('[aria-label="钱包分类"] [aria-current="page"]').text()).toBe('远程钱包');
});
