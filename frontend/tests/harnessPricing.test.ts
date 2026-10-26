import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import { mockPricingCatalog, mockWallet } from '../src/infra/bridge/mockPricing';
import SwitchPricingDialog from '../src/features/harness/SwitchPricingDialog.vue';
import CurrentPricingButton from '../src/features/harness/CurrentPricingButton.vue';
import VendorBalanceCard from '../src/features/harness/VendorBalanceCard.vue';
import HarnessPage from '../src/features/harness/HarnessPage.vue';
import { versionName } from '../src/features/harness/types';

const cleanup: (() => void)[] = [];
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T06:32:17.000Z')); });
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); document.body.innerHTML = ''; });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
function renderDialog(bridge: Bridge, catalog = mockPricingCatalog()) {
  const wrapper = mount(SwitchPricingDialog, { props: { harness: 'codex', open: true, catalog }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); return wrapper;
}
function renderBalance(bridge: Bridge, vendorId: string | null = 'vendor-standard') {
  const wrapper = mount(VendorBalanceCard, { props: { groupId: 'standard', vendorId, timezone: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); return wrapper;
}
const calls = (call: { mock: { calls: unknown[][] } }, name: string) => call.mock.calls.filter(([command]) => command === name);

describe('current pricing dialog', () => {
  it('uses label then vN for button/options, excludes disabled groups and disables unchanged confirmation', async () => {
    expect(versionName({ label: '秋季价格', number: 2 })).toBe('秋季价格'); expect(versionName({ label: '', number: 9 })).toBe('v9');
    const bridge = mock(); const catalog = mockPricingCatalog();
    const button = mount(CurrentPricingButton, { props: { harness: 'codex', catalog }, global: { provide: { [BridgeKey as symbol]: bridge } } });
    cleanup.push(() => button.unmount()); expect(button.text()).toContain('标准价格 · v1');
    await button.setProps({ harness: 'dsh' }); expect(button.text()).toContain('备用价格 · 当前方案');
    const wrapper = renderDialog(bridge); await flushPromises();
    expect(wrapper.findAll('select[aria-label="价格组"] option').map(option => option.text())).toEqual(['标准价格', '备用价格', '未关联价格']);
    expect(wrapper.findAll('select[aria-label="版本"] option').map(option => option.text())).toEqual(['v1（当前生效）', '秋季价格']);
    expect(wrapper.get('.button--primary').attributes('disabled')).toBeDefined(); expect(wrapper.text()).toContain('与当前设置相同');
  });
  it('updates versions on group changes and chooses the effective version even when it is not first', async () => {
    const bridge = mock(); const wrapper = renderDialog(bridge); await flushPromises();
    await wrapper.get('select[aria-label="价格组"]').setValue('alternate'); await flushPromises();
    expect(wrapper.findAll('select[aria-label="版本"] option').map(option => option.text())).toEqual(['v1', '当前方案（当前生效）']);
    expect((wrapper.get('select[aria-label="版本"]').element as HTMLSelectElement).value).toBe('alternate-v2');
    expect(wrapper.get('.button--primary').attributes('disabled')).toBeUndefined();
  });
  it('takes the affected Harness list and revision from the read-only preview; cancel makes zero writes', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge);
    const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'pricing.previewSwitch'
      ? { ok: true, data: { changed: true, revision: 18, affectedHarnesses: ['dsh'], groupId: null, versionId: null } } as never : original(name, payload));
    const wrapper = renderDialog(bridge); await flushPromises();
    await wrapper.get('select[aria-label="版本"]').setValue('standard-v2'); await flushPromises();
    expect(wrapper.text()).toContain('切换版本会影响使用此组的所有 Harness：Deepseek Harness');
    expect(wrapper.text()).toContain('已入账数据不会自动重算');
    expect(calls(call, 'pricing.previewSwitch').length).toBeGreaterThan(0);
    await wrapper.get('.button--secondary').trigger('click');
    expect(wrapper.emitted('update:open')).toEqual([[false]]);
    expect(call.mock.calls.filter(([, payload]) => 'operationId' in payload)).toHaveLength(0);
  });
  it('keeps failed selections and inline errors, retries with the original operationId and blocks pending closure', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge);
    let finish!: (value: unknown) => void; let reject = true;
    const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
      if (name === 'pricing.switchCurrent' && reject) return { ok: false, error: { code: 'WRITE_TIMEOUT', message: '超时', retryable: true } } as never;
      if (name === 'pricing.switchCurrent') return new Promise(resolve => { finish = resolve as never; });
      return original(name, payload);
    });
    const wrapper = renderDialog(bridge); await flushPromises();
    await wrapper.get('select[aria-label="价格组"]').setValue('alternate'); await flushPromises();
    await wrapper.get('.button--primary').trigger('click'); await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain('写入等待超时');
    expect((wrapper.get('select[aria-label="价格组"]').element as HTMLSelectElement).value).toBe('alternate');
    expect((wrapper.get('select[aria-label="版本"]').element as HTMLSelectElement).value).toBe('alternate-v2');
    expect(wrapper.emitted('switched')).toBeUndefined();
    reject = false; await wrapper.get('.button--primary').trigger('click');
    expect(calls(call, 'pricing.switchCurrent')[1]![1]).toEqual(calls(call, 'pricing.switchCurrent')[0]![1]);
    expect(wrapper.get('select').attributes('disabled')).toBeDefined(); expect(wrapper.get('button[aria-label="关闭"]').attributes('disabled')).toBeDefined();
    await wrapper.get('dialog').trigger('cancel'); expect(wrapper.emitted('update:open')).toBeUndefined();
    finish({ ok: true, data: { changed: true, revision: 2, affectedHarnesses: ['codex'], groupId: 'alternate', versionId: 'alternate-v2' } }); await flushPromises();
    expect(wrapper.emitted('switched')).toEqual([[{ groupId: 'alternate', versionId: 'alternate-v2' }]]);
    expect(wrapper.emitted('update:open')).toEqual([[false]]);
  });
  it('blocks confirmation during preview failures and discards older previews after a new selection', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge);
    let finish!: (value: unknown) => void;
    vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
      if (name === 'pricing.previewSwitch' && 'versionId' in payload && payload.versionId === 'standard-v2') return new Promise(resolve => { finish = resolve as never; });
      if (name === 'pricing.previewSwitch' && 'groupId' in payload && payload.groupId === 'unlinked') return { ok: false, error: { code: 'READ_TIMEOUT', message: '超时', retryable: true } } as never;
      return original(name, payload);
    });
    const wrapper = renderDialog(bridge); await flushPromises();
    await wrapper.get('select[aria-label="版本"]').setValue('standard-v2');
    expect(wrapper.get('.button--primary').attributes('disabled')).toBeDefined();
    await wrapper.get('select[aria-label="价格组"]').setValue('unlinked'); await flushPromises();
    finish({ ok: true, data: { changed: true, revision: 99, affectedHarnesses: ['dsh'], groupId: null, versionId: null } }); await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain('读取等待超时');
    expect(wrapper.get('.button--primary').attributes('disabled')).toBeDefined();
    expect(wrapper.text()).not.toContain('所有 Harness：Deepseek Harness');
  });
  it('refreshes a conflicting preview and preserves the draft for a fresh confirmation', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge); let conflict = true; let revision = 1;
    const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
      if (name === 'pricing.previewSwitch') { const response = unwrap(await original(name, payload as never)); return { ok: true, data: { ...response, revision } } as never; }
      if (name === 'pricing.switchCurrent' && conflict) { revision = 2; return { ok: false, error: { code: 'PRICING_REVISION_CONFLICT', message: 'conflict', retryable: false, conflictRevision: 2 } } as never; }
      if (name === 'pricing.switchCurrent') return { ok: true, data: { changed: true, revision: 3, groupId: 'alternate', versionId: 'alternate-v2', affectedHarnesses: ['codex'] } } as never;
      return original(name, payload);
    });
    const wrapper = renderDialog(bridge); await flushPromises();
    await wrapper.get('select[aria-label="价格组"]').setValue('alternate'); await flushPromises();
    await wrapper.get('.button--primary').trigger('click'); await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain('价格目录已更新');
    expect((wrapper.get('select[aria-label="版本"]').element as HTMLSelectElement).value).toBe('alternate-v2');
    conflict = false; await wrapper.get('.button--primary').trigger('click'); await flushPromises();
    expect(calls(call, 'pricing.switchCurrent')[1]![1]).toMatchObject({ expectedRevision: 2 });
    expect(wrapper.emitted('switched')).toHaveLength(1);
  });
});

describe('vendor balance card', () => {
  it.each([['1239000000000', '1.23'], ['-1239000000000', '-1.23'], ['9000000000', '0.00'], ['-9000000000', '0.00'], ['-500000000000', '-0.50']])('truncates %s to %s using the same money class', async (pico, text) => {
    const bridge = mock(); const wallet = mockWallet(); wallet.vendors[0]!.B = pico; wallet.vendors[0]!.coverageIncomplete = true;
    const original = bridge.call.bind(bridge);
    vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'wallet.list' ? { ok: true, data: wallet } as never : original(name, payload));
    const wrapper = renderBalance(bridge); await flushPromises();
    expect(wrapper.get('.money').text()).toBe(text); expect(wrapper.get('.money').classes()).toEqual(['money']);
    expect(wrapper.get('.balance-card').attributes('href')).toBe('#/wallet/vendor-standard');
    expect(wrapper.text()).not.toContain('最近校准'); expect(wrapper.text()).not.toContain('累计消费不完整');
  });
  it('shows --- for loading, unlinked or failed states with no zero fallback', async () => {
    const bridge = mock(); const call = vi.spyOn(bridge, 'call'); const wrapper = renderBalance(bridge, null); await flushPromises();
    expect(calls(call, 'wallet.list')).toHaveLength(0); expect(wrapper.text()).toContain('余额：---'); expect(wrapper.get('.money').text()).toBe('---'); expect(wrapper.find('a').exists()).toBe(false);
    call.mockResolvedValue({ ok: false, error: { code: 'READ_TIMEOUT', message: 'timeout', retryable: true } } as never);
    await wrapper.setProps({ vendorId: 'vendor-standard' }); await flushPromises();
    expect(wrapper.get('.money').text()).toBe('---'); expect(wrapper.attributes('title')).toContain('读取等待超时');
    await wrapper.setProps({ groupError: '目录不可用' }); expect(wrapper.get('.money').text()).toBe('---'); expect(wrapper.attributes('title')).toBe('目录不可用');
  });
  it('clears the previous balance before the new group read completes and rejects stale vendor replies', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge); const wrapper = renderBalance(bridge); await flushPromises();
    expect(wrapper.text()).toContain('12.33');
    let finish!: (value: unknown) => void;
    vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'wallet.list' ? new Promise(resolve => { finish = resolve as never; }) : original(name, payload));
    await wrapper.setProps({ groupId: 'alternate', vendorId: 'vendor-alternate' });
    expect(wrapper.get('.money').text()).toBe('---'); expect(wrapper.text()).not.toContain('示例供应商');
    await wrapper.setProps({ groupId: 'unlinked', vendorId: null }); await flushPromises();
    finish({ ok: true, data: mockWallet() }); await flushPromises();
    expect(wrapper.text()).toContain('余额：---'); expect(wrapper.text()).not.toContain('-1.23');
  });
  it('refreshes only relevant wallet changes and hides failed stale balances', async () => {
    const bridge = mock(); const original = bridge.call.bind(bridge); let failure = false;
    const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'wallet.list' && failure ? { ok: false, error: { code: 'READ_TIMEOUT', message: 'timeout', retryable: true } } as never : original(name, payload));
    const wrapper = renderBalance(bridge); await flushPromises(); expect(wrapper.get('.money').text()).toBe('12.33');
    bridge.receiveEvent('wallet.changed', { revision: 2, vendorIds: ['vendor-alternate'] }); await flushPromises(); expect(calls(call, 'wallet.list')).toHaveLength(1);
    failure = true; bridge.receiveEvent('wallet.changed', { revision: 3, vendorIds: ['vendor-standard'] }); await vi.advanceTimersByTimeAsync(5000); await flushPromises();
    expect(calls(call, 'wallet.list')).toHaveLength(2); expect(wrapper.get('.money').text()).toBe('---');
    expect(wrapper.attributes('title')).toContain('读取等待超时');
  });
});

it('successful page switch hides the old balance while the catalog is still pending', async () => {
  const bridge = mock(); const initialPreferences = unwrap(await bridge.call('preferences.get', {})); const original = bridge.call.bind(bridge);
  const wrapper = mount(HarnessPage, { props: { harness: 'codex', initialPreferences }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises(); expect(wrapper.get('.balance-card').text()).toContain('12.33');
  await wrapper.get('.pricing-button').trigger('click'); await flushPromises();
  await wrapper.get('select[aria-label="价格组"]').setValue('alternate'); await flushPromises();
  let blocked = true;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'pricing.catalog' && blocked ? new Promise(() => {}) : original(name, payload));
  await wrapper.get('.button--primary').trigger('click'); await flushPromises();
  expect(wrapper.get('.balance-card .money').text()).toBe('---'); expect(wrapper.get('.balance-card').text()).not.toContain('示例供应商');
  blocked = false; bridge.receiveEvent('pricing.changed', { revision: 2 }); await flushPromises();
  expect(wrapper.get('.balance-card .money').text()).toBe('-1.23'); expect(wrapper.get('.pricing-button').text()).toContain('备用价格 · 当前方案');
});

it('mock switches are atomic, affect shared Harness versions, preserve wallet totals and replay receipts', async () => {
  const bridge = mock(); const before = unwrap(await bridge.call('wallet.list', {}));
  const changed = vi.fn(); bridge.events.subscribe('pricing.changed', changed);
  const request = { harness: 'codex' as const, groupId: 'standard', versionId: 'standard-v2', expectedRevision: 1, operationId: crypto.randomUUID() };
  const preview = unwrap(await bridge.call('pricing.previewSwitch', { harness: request.harness, groupId: request.groupId, versionId: request.versionId })); expect(preview.affectedHarnesses).toEqual(['claude', 'codex', 'zcode']);
  expect(unwrap(await bridge.call('pricing.catalog', {})).revision).toBe(1);
  const result = await bridge.call('pricing.switchCurrent', request); expect(result).toMatchObject({ ok: true, data: { changed: true, revision: 2 } });
  expect(await bridge.call('pricing.switchCurrent', request)).toEqual(result); expect(changed).toHaveBeenCalledTimes(1);
  expect(unwrap(await bridge.call('pricing.catalog', {})).current.zcode?.versionId).toBe('standard-v2');
  expect(await bridge.call('wallet.list', {})).toEqual({ ok: true, data: before });
  expect(await bridge.call('pricing.switchCurrent', { ...request, versionId: 'standard-v1' })).toMatchObject({ ok: false, error: { code: 'OPERATION_ID_REUSED' } });
  expect(await bridge.call('pricing.switchCurrent', { ...request, operationId: crypto.randomUUID() })).toMatchObject({ ok: false, error: { code: 'PRICING_REVISION_CONFLICT' } });
  expect(await bridge.call('pricing.switchCurrent', { ...request, expectedRevision: 2, groupId: 'alternate', operationId: crypto.randomUUID() })).toMatchObject({ ok: false, error: { code: 'VERSION_GROUP_MISMATCH' } });
  expect(unwrap(await bridge.call('pricing.catalog', {})).revision).toBe(2);
  expect(unwrap(await bridge.call('pricing.switchCurrent', { ...request, expectedRevision: 2, operationId: crypto.randomUUID() })).changed).toBe(false);
  expect(changed).toHaveBeenCalledTimes(1);
});
