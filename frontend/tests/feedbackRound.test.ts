import { defineComponent, h } from 'vue';
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { useShellPreferences } from '../src/app/useShellPreferences';
import { createMockBridge } from '../src/infra/bridge/mock';
import TimeList from '../src/ui/TimeList.vue';
import ProviderScanDialog from '../src/features/wallet/ProviderScanDialog.vue';
import RemoteAccountDialog from '../src/features/wallet/RemoteAccountDialog.vue';
const cleanups: (() => void)[] = [];
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); vi.useRealTimers(); });
function bridge() { const result = createMockBridge(); cleanups.push(() => result.dispose()); return result; }

it('merges a setting after a concurrent background save without showing a conflict', async () => {
  const api = bridge(); const initial = unwrap(await api.call('preferences.get', {})); const original = api.call.bind(api); let raced = false;
  const spy = vi.spyOn(api, 'call').mockImplementation(async (command, payload) => {
    if (command === 'preferences.update' && !raced) { raced = true; await original('preferences.update', { patch: { theme: 'dark' }, expectedRevision: initial.revision, operationId: crypto.randomUUID() }); }
    return original(command, payload);
  });
  let preferences!: ReturnType<typeof useShellPreferences>;
  const host = mount(defineComponent({ setup() { preferences = useShellPreferences(initial); return () => h('div'); } }), { global: { provide: { [BridgeKey as symbol]: api } } }); cleanups.push(() => host.unmount());
  await preferences.save({ collapsed: true });
  const saved = unwrap(await api.call('preferences.get', {}));
  expect(saved.preferences).toMatchObject({ theme: 'dark', collapsed: true }); expect(preferences.error.value).toBe('');
  const writes = spy.mock.calls.filter(([name]) => name === 'preferences.update'); expect(writes).toHaveLength(2);
  expect(writes[0]![1]).toMatchObject({ expectedRevision: 0 }); expect(writes[1]![1]).toMatchObject({ expectedRevision: 1 });
});

it('time buttons contain only digits and wrap keyboard and scrolling at the end', async () => {
  const host = mount(TimeList, { props: { modelValue: 59, maximum: 59, label: '分钟' }, attachTo: document.body }); cleanups.push(() => host.unmount());
  expect(host.get('button').text()).toBe('59'); await host.get('button').trigger('click');
  await host.get('[data-index="119"]').trigger('keydown', { key: 'ArrowDown' });
  expect((document.activeElement as HTMLElement).dataset.value).toBe('0');
  await host.get('[data-index="60"]').trigger('keydown', { key: 'ArrowUp' });
  expect((document.activeElement as HTMLElement).dataset.value).toBe('59');
  const list = host.get('.options'); (list.element as HTMLElement).scrollTop = 60 * 34 * 2 + 20; await list.trigger('scroll');
  expect((list.element as HTMLElement).scrollTop).toBe(60 * 34 + 20);
  await host.get('[data-index="60"]').trigger('click'); expect(host.emitted('update:modelValue')).toEqual([[0]]); expect(host.find('.options').exists()).toBe(false);
});

it('requires names for every selected provider and creates separate wallets', async () => {
  vi.useFakeTimers(); const api = bridge();
  const host = mount(ProviderScanDialog, { global: { provide: { [BridgeKey as symbol]: api }, stubs: { teleport: true } } }); cleanups.push(() => host.unmount());
  await flushPromises(); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  const names = host.findAll('input:not([type=checkbox])'); expect(names).toHaveLength(2);
  const save = () => host.findAll('button').find(button => button.text().startsWith('创建选中供应商'))!;
  expect(save().attributes('disabled')).toBeDefined(); await names[0]!.setValue('供应商甲'); expect(save().attributes('disabled')).toBeDefined();
  await names[1]!.setValue('供应商乙'); expect(save().attributes('disabled')).toBeUndefined(); await save().trigger('click'); await flushPromises();
  const saved = unwrap(await api.call('wallet.list', {})); expect(saved.vendors.map(v => v.name)).toEqual(expect.arrayContaining(['供应商甲', '供应商乙'])); expect(host.emitted('saved')).toHaveLength(1);
});

it.each(['api_key', 'access_token'] as const)('normalizes copied %s credentials before saving', async authMode => {
  const api = bridge(); const created = unwrap(await api.call('wallet.createVendor', { name: 'Remote fixture', operationId: crypto.randomUUID() })); const spy = vi.spyOn(api, 'call');
  const host = mount(RemoteAccountDialog, { props: { vendorId: created.vendorId, vendorName: 'Remote fixture' }, global: { provide: { [BridgeKey as symbol]: api }, stubs: { teleport: true } } }); cleanups.push(() => host.unmount()); await flushPromises();
  await host.get('input[aria-label="站点地址"]').setValue('https://example.test');
  await host.get('select[aria-label="接口类型"]').setValue(authMode === 'access_token' ? 'newapi' : 'sub2api');
  if (authMode === 'access_token') { await host.get('select[aria-label="接口类型"]').setValue('newapi'); await host.get('input[aria-label="用户 ID"]').setValue('1'); }
  await host.get('input[aria-label="令牌"]').setValue('  Bearer synthetic-secret-abcd  '); await host.get('form').trigger('submit'); await flushPromises();
  expect(spy.mock.calls.find(([command]) => command === 'remote.save')?.[1]).toMatchObject({ credential: 'synthetic-secret-abcd', authMode }); expect(host.emitted('saved')).toHaveLength(1);
});
