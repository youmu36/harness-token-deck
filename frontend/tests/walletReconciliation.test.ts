import { flushPromises, mount } from '@vue/test-utils';
import { effectScope } from 'vue';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { parseRoute } from '../src/app/router';
import { createMockBridge } from '../src/infra/bridge/mock';
import { amountDraft, amountPico, calibrationSession } from '../src/features/wallet/calibrationDraft';
import WalletReconciliation from '../src/features/wallet/WalletReconciliation.vue';
import WalletEvidence from '../src/features/wallet/WalletEvidence.vue';
import WalletPage from '../src/features/wallet/WalletPage.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(stop => stop()); vi.restoreAllMocks(); localStorage.clear(); document.body.innerHTML = ''; });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
it('bounds inactive vendor drafts while retaining an open editor and an unpersisted uncertain write',()=>{
  const bridge=mock(), scope=effectScope();cleanup.push(()=>scope.stop());
  const active=scope.run(()=>calibrationSession(bridge,'active'))!;
  const oldest=calibrationSession(bridge,'oldest');oldest.draft.note='old';
  const uncertain=calibrationSession(bridge,'uncertain');
  uncertain.uncertain.value={operationId:'pending',action:'revoke',vendorId:'uncertain',fingerprint:'scope',targetId:'target',note:'authorized'} as never;
  for(let index=0;index<20;index++)calibrationSession(bridge,`vendor-${index}`);
  expect(calibrationSession(bridge,'active')).toBe(active);
  expect(calibrationSession(bridge,'uncertain')).toBe(uncertain);
  expect(calibrationSession(bridge,'oldest')).not.toBe(oldest);
  expect(calibrationSession(bridge,'oldest').draft.note).toBe('');
});
function workflow(bridge: Bridge, vendorId = 'vendor-standard') {
  const state = calibrationSession(bridge, vendorId);
  state.draft.note = '经核实的合成账单';
  state.draft.start = '2026-10-01T02:00:00.000Z'; state.draft.end = '2026-10-01T03:00:00.000Z';
  const wrapper = mount(WalletReconciliation, { props: { vendorId, mode: 'UTC' }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); return { wrapper, state };
}
async function click(wrapper: ReturnType<typeof workflow>['wrapper'], text: string) {
  await wrapper.findAll('button').find(button => button.text() === text)!.trigger('click'); await flushPromises();
}

it('closes validation errors without rereading completed scopes and clears them when leaving', async () => {
  const bridge = mock(), call = vi.spyOn(bridge,'call'), {wrapper,state} = workflow(bridge);
  await flushPromises(); await click(wrapper,'人工指定目标');
  state.draft.note='';
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(wrapper.text()).toContain('请填写金额依据');
  const reads = () => call.mock.calls.filter(([name]) => name.startsWith('reconciliation.')).length;
  const before = reads();
  await wrapper.get('[aria-label="关闭校准提醒"]').trigger('click'); await flushPromises();
  expect(wrapper.text()).not.toContain('请填写金额依据');expect(reads()).toBe(before);
  state.draft.note='已核对';await flushPromises();expect(reads()).toBe(before);
  state.draft.note='';await wrapper.get('form').trigger('submit');await flushPromises();
  await click(wrapper,'返回消费时段');expect(wrapper.text()).not.toContain('请填写金额依据');
  expect(reads()).toBe(before);
});

it('ignores a late preview failure after leaving the editor', async () => {
  const bridge=mock(), original=bridge.call.bind(bridge), {wrapper,state}=workflow(bridge);
  let reject!: (reason:Error) => void;
  vi.spyOn(bridge,'call').mockImplementation((name,payload) => name==='reconciliation.previewCalibration' ? new Promise((_,fail) => {reject=fail;}) as never : original(name,payload));
  await flushPromises();await click(wrapper,'人工指定目标');state.draft.target=amountDraft('1000000000000');
  await wrapper.get('form').trigger('submit');await flushPromises();
  await click(wrapper,'返回消费时段');reject(new Error('过期预览失败'));await flushPromises();
  expect(wrapper.text()).not.toContain('过期预览失败');
});

it('previews and commits exact targets, then revokes using the current estimate', async () => {
  const bridge = mock(), call = vi.spyOn(bridge, 'call'); const { wrapper, state } = workflow(bridge);
  await flushPromises(); await click(wrapper, '人工指定目标');
  state.draft.target = amountDraft('1000000000001'); state.draft.confirmScope = true;
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(wrapper.get('[aria-label="校准预览"]').text()).toContain('本地余额变化');
  await click(wrapper, '确认采用目标');
  expect(call.mock.calls.find(([name]) => name === 'reconciliation.commitCalibration')?.[1]).toMatchObject({ targetPico: '1000000000001' });
  expect(unwrap(await bridge.call('reconciliation.preview', { vendorId: 'vendor-standard', start: state.draft.start, end: state.draft.end })).currentEffectivePico).toBe('1000000000001');
  expect(wrapper.text()).toContain('已锁定');
  await click(wrapper, '撤销目标'); await wrapper.get('form').trigger('submit'); await flushPromises();
  await click(wrapper, '确认撤销');
  expect(unwrap(await bridge.call('reconciliation.preview', { vendorId: 'vendor-standard', start: state.draft.start, end: state.draft.end })).currentEffectivePico).toBe('700000000000');
  expect(wrapper.text()).toContain('已撤销');
});

it('invalidates a stale preview and keeps drafts after a conflict', async () => {
  const bridge = mock(), { wrapper, state } = workflow(bridge); await flushPromises();
  await click(wrapper, '人工指定目标'); state.draft.target = amountDraft('1000000000000'); state.draft.confirmScope = true;
  await wrapper.get('form').trigger('submit'); await flushPromises(); expect(state.preview.value).toBeDefined();
  await wrapper.get('[aria-label="金额依据"]').setValue('补充证据'); expect(state.preview.value).toBeUndefined();
  await wrapper.get('form').trigger('submit'); await flushPromises();
  bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 2 }); expect(state.preview.value).toBeDefined();
  const original = bridge.call.bind(bridge);
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'reconciliation.commitCalibration'
    ? { ok: false, error: { code: 'CALIBRATION_PLAN_CHANGED', message: '核对证据已变化，请重新预览', retryable: false } } as never : original(name, payload));
  await wrapper.get('form').trigger('submit'); await flushPromises(); await click(wrapper, '确认采用目标');
  expect(state.draft.note).toBe('补充证据'); expect(wrapper.text()).toContain('请重新预览'); expect(state.preview.value).toBeUndefined();
});

it('reuses the authorized operation after hiding and a timeout without charging twice', async () => {
  const bridge = mock(), original = bridge.call.bind(bridge); let timeout = true;
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'reconciliation.commitCalibration' && timeout) { timeout = false; return { ok: false, error: { code: 'WRITE_TIMEOUT', message: '超时', retryable: true } } as never; }
    return result;
  });
  const { wrapper, state } = workflow(bridge); await flushPromises(); await click(wrapper, '人工指定目标');
  state.draft.target = amountDraft('1000000000000'); state.draft.confirmScope = true;
  await wrapper.get('form').trigger('submit'); await flushPromises(); await click(wrapper, '确认采用目标');
  const before = unwrap(await bridge.call('wallet.list', {})).vendors.find(row => row.id === 'vendor-standard')!.C;
  const operation = state.uncertain.value!.operationId; expect(localStorage.length).toBe(1);
  wrapper.unmount(); const restored = workflow(bridge).wrapper; await flushPromises();
  expect(restored.text()).toContain('上次操作的结果尚未确认'); await click(restored, '查询或重试上次操作');
  expect(call.mock.calls.filter(([name]) => name === 'reconciliation.commitCalibration').map(([, payload]) => (payload as { operationId: string }).operationId)).toEqual([operation, operation]);
  expect(unwrap(await bridge.call('wallet.list', {})).vendors.find(row => row.id === 'vendor-standard')!.C).toBe(before);
  expect(localStorage.length).toBe(0);
});

it('scopes missing evidence to one vendor or unassigned, and confirms only the current evidence', async () => {
  const bridge = mock(), call = vi.spyOn(bridge, 'call');
  const wrapper = mount(WalletEvidence, { props: { vendorId: '', mode: 'UTC' }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount());
  await flushPromises(); expect(wrapper.text()).toContain('至少 1');
  expect(call.mock.calls.find(([name]) => name === 'stats.explore')?.[1]).toMatchObject({ vendorIds: [''], sections: ['quality'] });
  await wrapper.findAll('button').find(row => row.text() === '确认已知缺失')!.trigger('click'); await flushPromises();
  expect(wrapper.get('[aria-label="钱包缺失证据"]').text()).toContain('没有记录');
  await wrapper.get('[aria-label="缺失确认状态"]').setValue('acknowledged');
  await wrapper.findAll('button').find(row => row.text() === '应用筛选')!.trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('已确认当前证据');
  await wrapper.get('[aria-label="缺失原因"]').setValue('unpriced');
  await wrapper.findAll('button').find(row => row.text() === '应用筛选')!.trigger('click'); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore').at(-1)?.[1]).toMatchObject({ qualityReasons: ['unpriced'], vendorIds: [''] });
});

it('keeps old routes usable while removing missing and remote comparison panels', async () => {
  expect(parseRoute('#/wallet/vendor-standard/reconcile')).toMatchObject({ page: 'wallet', detail: 'vendor-standard', walletSection: 'reconcile' });
  expect(parseRoute('#/wallet/vendor-standard/allocate')?.walletSection).toBe('allocate');
  expect(parseRoute('#/wallet/unassigned')).toMatchObject({ page: 'wallet', walletSection: 'unassigned' });
  const bridge = mock(); const wrapper = mount(WalletPage, { props: { initialPreferences: unwrap(await bridge.call('preferences.get', {})), walletSection: 'unassigned' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.find('[aria-label="未归属记录"]').exists()).toBe(false); expect(wrapper.find('[aria-label="供应商二级导航"]').exists()).toBe(true); expect(wrapper.text()).not.toContain('远程对照'); expect(wrapper.text()).not.toContain('缺失记录');
});

it('opens the independent allocation form when the route changes within the same vendor', async () => {
  const bridge = mock(), { wrapper } = workflow(bridge); await flushPromises();
  expect(wrapper.find('form').exists()).toBe(false);
  await wrapper.setProps({ manual: true }); await flushPromises();
  expect(wrapper.get('form').text()).toContain('设置校准金额');
});

it('preserves pico for untouched amounts and validates edited precision', () => {
  expect(amountPico(amountDraft('1'))).toBe('1');
  const amount = amountDraft('1000000000001'); amount.text = '1.000001'; expect(amountPico(amount)).toBe('1000001000000');
  expect(() => amountPico({ text: '-0.1', originalText: '' })).toThrow();
});

it('reads only the account list until a period is selected and does not poll on collection bursts', async () => {
  const bridge = mock(), call = vi.spyOn(bridge, 'call'), {wrapper,state} = workflow(bridge);
  await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'reconciliation.preview')).toHaveLength(0);
  expect(call.mock.calls.filter(([name]) => name === 'reconciliation.targets')).toHaveLength(0);
  for(let revision=1;revision<=30;revision++) {
    bridge.receiveEvent('usage.changed',{harnesses:['codex'],revision});
    bridge.receiveEvent('wallet.changed',{vendorIds:['vendor-standard'],revision});
    bridge.receiveEvent('cost.changed',{revision});
    await flushPromises();
  }
  expect(call.mock.calls.filter(([name]) => name === 'reconciliation.batches')).toHaveLength(1);
  expect(call.mock.calls.filter(([name]) => name === 'reconciliation.preview')).toHaveLength(0);
  expect(wrapper.get('[aria-label="账户消费时段"]').text()).toContain('$1.0000');
  await click(wrapper,'校准本地消费');
  expect(call.mock.calls.filter(([name]) => name === 'reconciliation.preview')).toHaveLength(1);
  expect(state.draft.intervalIds).toEqual(['remote-example']);
  expect(state.draft.target.originalPico).toBe('1000000000000');
  state.draft.confirmScope=true;
  await wrapper.get('form').trigger('submit');await flushPromises();
  expect(wrapper.get('[aria-label="模型校准分配"]').text()).toContain('gpt-6');
  expect(call.mock.calls.find(([name])=>name==='reconciliation.previewCalibration')?.[1]).toMatchObject({basis:'remote',allocationMode:'proportional'});
});

it('submits explicit daily targets and model evidence without proportional allocation', async () => {
  const bridge = mock(), call = vi.spyOn(bridge, 'call'), { wrapper, state } = workflow(bridge);
  await flushPromises(); await click(wrapper, '人工指定目标');
  state.draft.confirmScope = true; state.draft.target = amountDraft('3000000000001');
  state.draft.allocationMode = 'explicit';
  state.draft.days = [{ day: '2026-10-01', amount: amountDraft('1000000000001') }, { day: '2026-10-02', amount: amountDraft('2000000000000') }];
  state.draft.allocations = [{ day: '2026-10-01', amount: amountDraft('500000000001'), model: 'gpt-6', harness: 'codex', group: '', version: '', session: '', confirm: true }];
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(call.mock.calls.find(([name]) => name === 'reconciliation.previewCalibration')?.[1]).toMatchObject({
    targetPico: '3000000000001', dayTargets: [{ day: '2026-10-01', targetPico: '1000000000001' }, { day: '2026-10-02', targetPico: '2000000000000' }],
    allocations: [{ day: '2026-10-01', targetPico: '500000000001', modelKeys: ['gpt-6'], harnesses: ['codex'], confirmModelCoverage: true }],
  });
  expect(wrapper.get('[aria-label="校准预览"]').text()).toContain('未明确分配');
});
