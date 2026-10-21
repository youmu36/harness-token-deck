import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type CommandData } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import CacheMonitor from '../src/features/harness/CacheMonitor.vue';
import ReconciliationNotice from '../src/features/reconciliation/ReconciliationNotice.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach(dispose => dispose()); vi.restoreAllMocks(); document.body.innerHTML = ''; });

it('keeps a permanent session selector, orders by opening time and selects every cache ratio locally', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  let rates: CommandData<'stats.cacheMonitor'>['sessionHitRates'] = [
    { sessionKey: '2222222222222222', displayName: 'Later', startedAt: '2026-10-20T00:01:00.000Z', lastActivityAt: '2026-10-20T00:03:00.000Z', cacheHitRate: 0.7, cacheCoverageIncomplete: false, frozen: false },
    { sessionKey: '1111111111111111', displayName: 'First', startedAt: '2026-10-20T00:00:00.000Z', lastActivityAt: '2026-10-20T00:02:00.000Z', cacheHitRate: 0.4, cacheCoverageIncomplete: false, frozen: false },
  ];
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'stats.cacheMonitor' && result.ok) Object.assign(result.data, { sessionHitRates: rates });
    return result;
  });
  const wrapper = mount(CacheMonitor, { props: { harness: 'codex', mode: 'minute', timezone: 'UTC' }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const trigger = () => wrapper.get('.session-rate .choice-trigger');
  expect(trigger().text().replace(/\s+/g, ' ')).toBe('会话1 缓存命中 40.0% 监测中');
  expect(wrapper.get('.session-rate [role=tooltip]').text()).toContain('本数值表示当前会话缓存命中率');
  const reads = call.mock.calls.length;
  await trigger().trigger('click');
  const options = wrapper.findAll('.session-rate [role="radio"]');
  expect(options.map(option => option.text().replace(/\s+/g, ' '))).toEqual([expect.stringContaining('会话1 First'), expect.stringContaining('会话2 Later')]);
  await options[1]!.trigger('click');
  expect(trigger().text().replace(/\s+/g, ' ')).toBe('会话2 缓存命中 70.0% 监测中');
  expect(trigger().attributes('aria-expanded')).toBe('false');
  await trigger().trigger('click'); await trigger().trigger('keydown', { key: 'Escape' });
  expect(trigger().attributes('aria-expanded')).toBe('false');
  expect(call.mock.calls.length).toBe(reads);
  rates = [{ ...rates[0]!, frozen: true }, rates[1]!];
  await wrapper.vm.refresh(); await flushPromises();
  expect(trigger().text().replace(/\s+/g, ' ')).toContain('会话1 缓存命中 40.0% 监测中');
  await trigger().trigger('click'); expect(wrapper.findAll('.session-rate [role="radio"]')).toHaveLength(1);
  rates = []; await wrapper.vm.refresh(); await flushPromises();
  expect(trigger().text().replace(/\s+/g, ' ')).toContain('暂无会话数据');
});

it('dismisses a displayed observation through IPC and allows a newer notice to appear', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  let notices: CommandData<'reconciliation.status'>['notices'] = [{ vendorId: 'synthetic-vendor', vendorName: 'Supplier', harness: 'codex', selectedGroupId: 'g', selectedGroupName: 'Other', observedAt: '2026-10-20T00:00:00.000Z', remoteDecreasePico: '1000000000000' }];
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    if (name === 'reconciliation.dismiss') { notices = []; return { ok: true, data: { notices } } as never; }
    if (name === 'reconciliation.status') return { ok: true, data: { notices } } as never;
    return original(name, payload);
  });
  const wrapper = mount(ReconciliationNotice, { props: { harness: 'codex' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const notice = notices[0]!;
  await wrapper.get('button[aria-label="关闭 Supplier 的供应商校准提醒"]').trigger('click'); await flushPromises();
  expect(call).toHaveBeenCalledWith('reconciliation.dismiss', { vendorId: notice.vendorId, observedAt: notice.observedAt });
  expect(wrapper.text()).toBe('');
  notices = [{ ...notice, observedAt: '2026-10-20T00:05:00.000Z' }];
  bridge.receiveEvent('wallet.changed', { revision: 2, vendorIds: ['synthetic-vendor'] }); await flushPromises();
  expect(wrapper.text()).toContain('Supplier');
});
