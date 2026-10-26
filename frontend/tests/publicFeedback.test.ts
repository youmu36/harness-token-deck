import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type CommandData } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import CacheMonitor from '../src/features/harness/CacheMonitor.vue';
import ReconciliationNotice from '../src/features/reconciliation/ReconciliationNotice.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach(dispose => dispose()); vi.restoreAllMocks(); vi.useRealTimers(); document.body.innerHTML = ''; });

it('keeps metric session selection while chart selection remains independent', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const call = vi.spyOn(bridge, 'call');
  const wrapper = mount(CacheMonitor, { props: { harness: 'codex', timezone: 'UTC' }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const trigger = () => wrapper.get('.session-info .choice-trigger');
  expect(trigger().text()).toContain('命中率');
  expect(trigger().text()).not.toContain('预览会话');
  await trigger().trigger('click');
  await wrapper.findAll('.session-info [role="radio"]')[1]!.trigger('click'); await flushPromises();
  expect(trigger().text()).toContain('命中率');
  expect(call.mock.calls.filter(([name]) => name === 'stats.cacheMonitor').at(-1)?.[1]).toMatchObject({ sessionRef: '123456789abcdef0' });
  expect(wrapper.find('[aria-label="缓存监控范围"]').exists()).toBe(false);
  expect(wrapper.text()).not.toContain('全程输入'); expect(wrapper.text()).not.toContain('活动批次命中');
  await wrapper.vm.refresh(); await flushPromises();
  expect(trigger().text()).toContain('命中率');
  await trigger().trigger('click'); await trigger().trigger('keydown', { key: 'Escape' });
  expect(trigger().attributes('aria-expanded')).toBe('false');
});

it('dismisses a displayed observation through IPC and allows a newer notice to appear', async () => {
  vi.useFakeTimers();
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
  bridge.receiveEvent('wallet.changed', { revision: 2, vendorIds: ['synthetic-vendor'] }); await vi.advanceTimersByTimeAsync(60000); await flushPromises();
  expect(wrapper.text()).toContain('Supplier');
});
