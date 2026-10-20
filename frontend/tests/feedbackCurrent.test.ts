import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createPreferenceState } from '../src/app/preferenceState';
import { createMockBridge } from '../src/infra/bridge/mock';
import Background from '../src/features/settings/Background.vue';
import WalletDialog from '../src/features/wallet/WalletDialog.vue';
import TimeRangeBar from '../src/features/usage-common/TimeRangeBar.vue';
import { calculateRange } from '../src/features/usage-common/useTimeRange';
import { defaultFilter } from '../src/features/usage-common/types';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); });
function bridge() { const value = createMockBridge(); cleanup.push(() => value.dispose()); return value; }

it('creates a vendor with explicit zero balance and separate editable past funding', async () => {
  const value = bridge(); const spy = vi.spyOn(value, 'call');
  const wrapper = mount(WalletDialog, { props: { action: 'create', walletRevision: 1, refresh: async () => {}, latest: () => undefined }, global: { provide: { [BridgeKey as symbol]: value }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount());
  await wrapper.get('[aria-label="名称"]').setValue('Imported');
  await wrapper.get('[aria-label="金额"]').setValue('0');
  await wrapper.get('[aria-label="过往充值金额"]').setValue('123.456789');
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(spy.mock.calls.find(([name]) => name === 'wallet.createVendor')?.[1]).toMatchObject({ initialBalance: '0', pastTopUp: '123.456789' });
  const created = unwrap(await value.call('wallet.list', {})).vendors.find(v => v.name === 'Imported')!;
  expect([created.R, created.B, created.pastTopUpPico]).toEqual(['0', '0', '123456789000000']);
  expect(created.lastCalibratedAt).not.toBeNull();
});

it('persists close-to-tray and startup choices independently of collection pause', async () => {
  const value = bridge(); const initial = unwrap(await value.call('preferences.get', {}));
  const wrapper = mount(Background, { props: { initial }, global: { provide: { [BridgeKey as symbol]: value } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const controls = wrapper.findAll('input[type="checkbox"]');
  expect((controls[0]!.element as HTMLInputElement).checked).toBe(true);
  expect((controls[1]!.element as HTMLInputElement).checked).toBe(false);
  await controls[0]!.setValue(false); await flushPromises();
  await controls[1]!.setValue(true); await flushPromises();
  expect(unwrap(await value.call('preferences.get', {})).preferences).toMatchObject({ closeToTray: false, startAtLogin: true });
  expect(unwrap(await value.call('collection.status', {})).paused).toBe(false);
});

it('offers a moving custom endpoint on Harness pages and preserves the analysis calendar', async () => {
  vi.useFakeTimers(); const now = new Date('2026-10-17T10:00:00Z'); vi.setSystemTime(now);
  const filter = { ...defaultFilter, range: 'custom' as const, start: '2026-10-16T10:00:00Z', end: now.toISOString() };
  const props = { modelValue: 'custom' as const, range: calculateRange(filter, 'UTC', now), mode: 'UTC' as const, customStart: filter.start, customEnd: filter.end };
  const wrapper = mount(TimeRangeBar, { props: { ...props, allowFollow: true }, attachTo: document.body });
  cleanup.push(() => wrapper.unmount());
  await wrapper.findAll('[aria-label="打开日历"]')[1]!.trigger('click'); await flushPromises();
  const action = wrapper.findAll('.calendar__actions button').find(b => b.text() === '随当前时间变动')!;
  expect(action.exists()).toBe(true); await action.trigger('click'); await flushPromises();
  await wrapper.get('form').trigger('submit');
  expect(wrapper.emitted('apply')?.at(-1)?.[0]).toMatchObject({ endNow: true, startNow: false });
  const later = new Date('2026-10-17T10:05:00Z');
  expect(calculateRange({ ...filter, endNow: true }, 'UTC', later).end).toBe(later.toISOString());
  const analysis = mount(TimeRangeBar, { props, attachTo: document.body }); cleanup.push(() => analysis.unmount());
  await analysis.findAll('[aria-label="打开日历"]')[1]!.trigger('click'); await flushPromises();
  expect(analysis.findAll('.calendar__actions button').map(b => b.text())).toEqual(['清除', '当前时间', '今天']);
});

it('coalesces queued preferences, merges page filters and flushes the final values', async () => {
  const value = bridge(); const initial = unwrap(await value.call('preferences.get', {})); const spy = vi.spyOn(value, 'call');
  const state = createPreferenceState(value, initial); cleanup.push(state.dispose);
  const first = state.save({ lastRoute: '/models' });
  const second = state.save({ lastRoute: '/harness/codex', filters: { codex: { ...defaultFilter, range: '24hours' } } });
  const third = state.save({ filters: { overview: { ...defaultFilter, range: '7days' } } });
  await state.flush(); await Promise.all([first, second, third]);
  const requests = spy.mock.calls.filter(([name]) => name === 'preferences.update');
  expect(requests).toHaveLength(1);
  expect(state.snapshot.value.preferences.lastRoute).toBe('/harness/codex');
  expect(state.snapshot.value.preferences.filters.codex?.range).toBe('24hours');
  expect(state.snapshot.value.preferences.filters.overview?.range).toBe('7days');
});
