import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import WalletPage from '../src/features/wallet/WalletPage.vue';
import WalletDialog from '../src/features/wallet/WalletDialog.vue';
import VendorCard from '../src/features/wallet/VendorCard.vue';
import VendorEntries from '../src/features/wallet/VendorEntries.vue';
import { toasts, dismiss } from '../src/ui/toastState';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); toasts.value.forEach(t => dismiss(t.id)); vi.restoreAllMocks(); });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
async function vendor(bridge: Bridge) { return unwrap(await bridge.call('wallet.list', {})).vendors[0]!; }
async function dialog(action: 'topup' | 'calibrate' | 'delete' | 'link', bridge = mock()) {
  const v = await vendor(bridge); const wallet = unwrap(await bridge.call('wallet.list', {}));
  let latest = v;
  const wrapper = mount(WalletDialog, { props: { action, vendor: v, walletRevision: wallet.revision, catalog: unwrap(await bridge.call('pricing.catalog', {})), refresh: async () => { latest = await vendor(bridge); }, latest: () => latest }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises(); return { wrapper, bridge, v };
}
it.each([['100000000000000', '30000000000000', '0', '70000000000000'], ['100000000000000', '30000000000000', '-5000000000000', '65000000000000'], ['120000000000000', '32000000000000', '-5000000000000', '83000000000000']])('displays W-01 through W-03 totals without an initial amount field', async (R, C, A, B) => {
  const bridge = mock(); const v = { ...await vendor(bridge), R, C, A, B };
  const wrapper = mount(VendorCard, { props: { vendor: v, mode: 'UTC' } }); cleanup.push(() => wrapper.unmount());
  expect(wrapper.text()).not.toContain('起始金额'); expect(wrapper.get('.balance').text()).toContain(`$${BigInt(B) / 1_000_000_000_000n}.0000`);
  expect(wrapper.findAll('dd')).toHaveLength(6); expect(wrapper.text()).toContain('过往消费');
});
it('calibration updates the live difference, refreshes conflicts and requires a fresh confirmation', async () => {
  const { wrapper, bridge, v } = await dialog('calibrate'); const call = vi.spyOn(bridge, 'call');
  await wrapper.get('input[aria-label="金额"]').setValue('10');
  expect(wrapper.text()).toContain('将追加调整 -$2.3390');
  await bridge.call('wallet.topUp', { vendorId: v.id, amount: '1', operationId: crypto.randomUUID() });
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(wrapper.text()).toContain('余额已变化（新余额 $13.3390），请确认后重试');
  expect((wrapper.get('input').element as HTMLInputElement).value).toBe('10');
  expect(wrapper.emitted('saved')).toBeUndefined();
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(wrapper.emitted('saved')).toHaveLength(1); expect((await vendor(bridge)).B).toBe('10000000000000');
  const writes = call.mock.calls.filter(([name]) => name === 'wallet.calibrate');
  expect(writes).toHaveLength(2); expect(writes[0]![1]).not.toEqual(writes[1]![1]);
});
it('top-up shows a toast, preserves failures and reuses the original operationId', async () => {
  const bridge = mock(); const original = bridge.call.bind(bridge); let fail = true;
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'wallet.topUp' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : original(name, payload));
  const { wrapper } = await dialog('topup', bridge); await wrapper.get('input').setValue('20');
  await wrapper.get('form').trigger('submit'); await flushPromises(); expect(wrapper.text()).toContain('写入等待超时');
  expect((wrapper.get('input').element as HTMLInputElement).value).toBe('20'); fail = false;
  await wrapper.get('form').trigger('submit'); await flushPromises();
  const writes = call.mock.calls.filter(([name]) => name === 'wallet.topUp'); expect(writes[0]![1]).toEqual(writes[1]![1]);
  expect(toasts.value.at(-1)?.message).toBe('已充值 $20.0000');
});
it('funded deletion explains archival and retains read-only records; links explain future attribution', async () => {
  const { wrapper, bridge, v } = await dialog('delete'); expect(wrapper.text()).toContain('该供应商有资金记录，将归档');
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(unwrap(await bridge.call('wallet.list', {})).vendors.some(item => item.id === v.id)).toBe(false);
  expect(unwrap(await bridge.call('wallet.list', { includeArchived: true })).vendors.find(item => item.id === v.id)?.status).toBe('archived');
  expect(unwrap(await bridge.call('wallet.entries', { vendorId: v.id })).entries.length).toBeGreaterThan(0);
  expect(await bridge.call('wallet.topUp', { vendorId: v.id, amount: '1', operationId: crypto.randomUUID() })).toMatchObject({ ok: false, error: { code: 'VENDOR_ARCHIVED' } });
  const links = await dialog('link'); expect(links.wrapper.text()).toContain('关联只影响之后入账的请求');
});
it('mock wallets use exact cumulative totals and stable entry pagination', async () => {
  const bridge = mock(); const created = unwrap(await bridge.call('wallet.createVendor', { name: 'Negative', initialBalance: '-0.5', operationId: crypto.randomUUID() }));
  const id = created.vendorId; expect(created.vendor).toMatchObject({ R: '0', C: '0', A: '-500000000000', B: '-500000000000' });
  const payload = { vendorId: id, amount: '100.123456', operationId: crypto.randomUUID() };
  const result = await bridge.call('wallet.topUp', payload); expect(await bridge.call('wallet.topUp', payload)).toEqual(result);
  const pricing = unwrap(await bridge.call('pricing.catalog', {}));
  expect(await bridge.call('pricing.createGroup', { name: 'Cross-command', expectedRevision: pricing.revision, operationId: payload.operationId })).toMatchObject({ ok: false, error: { code: 'OPERATION_ID_REUSED' } });
  expect(unwrap(result).vendor?.B).toBe('99623456000000');
  await bridge.call('wallet.calibrate', { vendorId: id, target: '90', expectedWalletRevision: unwrap(result).revision, operationId: crypto.randomUUID() });
  const page = unwrap(await bridge.call('wallet.entries', { vendorId: id, limit: 1 }));
  expect(page.nextCursor).not.toBeNull();
  expect(page.entries[0]).toMatchObject({ kind: 'adjustment', amountPico: '-9623456000000' });
  const next = unwrap(await bridge.call('wallet.entries', { vendorId: id, limit: 1, cursor: page.nextCursor })); expect(next.entries[0]?.kind).toBe('top_up');
});
it('entry view loads further pages without duplicates', async () => {
  const bridge = mock(); const v = await vendor(bridge);
  for (let i = 0; i < 50; i++) await bridge.call('wallet.topUp', { vendorId: v.id, amount: '1', operationId: crypto.randomUUID() });
  const wrapper = mount(VendorEntries, { props: { vendor: v, mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.findAll('tbody tr')).toHaveLength(50); await wrapper.findAll('button').find(button => button.text() === '加载更多')!.trigger('click'); await flushPromises(); expect(wrapper.findAll('tbody tr')).toHaveLength(52);
});
it('page supports detail routes and archived folding', async () => {
  const bridge = mock(); const v = await vendor(bridge); const wrapper = mount(WalletPage, { props: { detail: v.id, initialPreferences: unwrap(await bridge.call('preferences.get', {})) }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.text()).toContain('资金流水'); expect(wrapper.text()).toContain(v.name);
});

it('entry view lists corrections separately with help in the headings', async () => {
  const bridge = mock(); const created = unwrap(await bridge.call('wallet.createVendor', { name: 'Daily spending', initialBalance: '100', operationId: crypto.randomUUID() }));
  let revision = created.revision;
  for (const target of ['70', '65', '65']) revision = unwrap(await bridge.call('wallet.calibrate', { vendorId: created.vendorId, target, expectedWalletRevision: revision, operationId: crypto.randomUUID() })).revision;
  const v = unwrap(await bridge.call('wallet.list', {})).vendors.find(item => item.id === created.vendorId)!;
  const wrapper = mount(VendorEntries, { props: { vendor: v, mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.findAll('tbody tr')).toHaveLength(3);
  expect(wrapper.text()).toContain('余额校准');
  expect(wrapper.findAll('thead [aria-label="金额含义"]')).toHaveLength(1);
  expect(wrapper.findAll('tbody [aria-label="金额含义"]')).toHaveLength(0);
  await wrapper.get('select').setValue('consumption_daily'); await flushPromises(); expect(wrapper.text()).toContain('暂无资金流水');
  await wrapper.get('select').setValue('adjustment'); await flushPromises(); expect(wrapper.findAll('tbody tr')).toHaveLength(3);
});
