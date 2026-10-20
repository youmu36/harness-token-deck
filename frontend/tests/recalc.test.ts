import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import RecalcDialog from '../src/features/recalc/RecalcDialog.vue';
import DateTimeField from '../src/ui/DateTimeField.vue';
import { BridgeKey, type Bridge, type RequestMap } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import { unwrap } from '../src/app/errors';
import { formatDateTimeInput } from '../src/shared/time';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });
function button(wrapper: VueWrapper, text: string) { return wrapper.findAll('button').find(b => b.text() === text)!; }
async function page(bridge: Bridge = createMockBridge()) {
  const wrapper = mount(RecalcDialog, { attachTo: document.body, props: { harness: 'codex', mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises(); return { wrapper, bridge };
}
async function preview(wrapper: VueWrapper) { await button(wrapper, '预览').trigger('click'); await flushPromises(); }
async function confirm(wrapper: VueWrapper) { await button(wrapper, '确认重算').trigger('click'); await flushPromises(); }
it('defaults to seventy-two hours with a five-minute margin and the current group active version', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T12:30:45.000Z')); const { wrapper, bridge } = await page(); const spy = vi.spyOn(bridge, 'call');
  expect((wrapper.get('input[aria-label="重算开始时间"]').element as HTMLInputElement).value).toBe('2026-10-03 12:35');
  expect((wrapper.get('select[aria-label="重算目标版本"]').element as HTMLSelectElement).value).toBe('standard-v1'); expect(wrapper.get('select[aria-label="重算价格分组"]').findAll('option')).toHaveLength(5);
  await preview(wrapper); expect(spy.mock.calls.find(([name]) => name === 'maintenance.previewRecalc')![1]).toMatchObject({ start: '2026-10-03T12:35:45.000Z', end: '2026-10-06T12:30:45.000Z', groupId: 'standard', versionId: 'standard-v1' });
});
it('shows precise costs, quality counts and original vendor changes before confirmation', async () => {
  const { wrapper, bridge } = await page(); const before = unwrap(await bridge.call('wallet.list', {})); const spy = vi.spyOn(bridge, 'call');
  await wrapper.get('select[aria-label="重算目标版本"]').setValue('standard-v2'); await preview(wrapper); expect(wrapper.text()).toContain('原成本'); expect(wrapper.text()).toContain('成本差额'); expect(wrapper.text()).toContain('未定价 / 不完整'); expect(wrapper.text()).toContain('示例供应商'); expect(wrapper.text()).toContain('本地余额投影变化');
  await confirm(wrapper); expect(wrapper.text()).toContain('不可撤销'); expect(spy.mock.calls.some(([name]) => name === 'maintenance.commitRecalc')).toBe(false);
  await wrapper.get('.button--danger').trigger('click'); await flushPromises(); expect(wrapper.emitted('committed')).toHaveLength(1);
  const after = unwrap(await bridge.call('wallet.list', {})); expect(BigInt(after.vendors[0]!.C)).toBeGreaterThan(BigInt(before.vendors[0]!.C));
  expect(BigInt(after.vendors[0]!.B)).toBeLessThan(BigInt(before.vendors[0]!.B));
});
it('changing a version or even an invalid uncommitted date draft clears the preview', async () => {
  const { wrapper } = await page(); await preview(wrapper); expect(wrapper.find('.recalc-preview').exists()).toBe(true);
  await wrapper.get('select[aria-label="重算价格分组"]').setValue('alternate'); expect(wrapper.find('.recalc-preview').exists()).toBe(false);
  await preview(wrapper); await wrapper.get('input[aria-label="重算开始时间"]').setValue('invalid'); expect(wrapper.find('.recalc-preview').exists()).toBe(false); expect(button(wrapper, '确认重算').attributes('disabled')).toBeDefined();
});
it('late preview responses cannot restore a plan after a parameter change', async () => {
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge); let resolve: ((value: unknown) => void) | undefined;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'maintenance.previewRecalc' ? await new Promise<unknown>(r => { resolve = r; }) as never : original(name, payload));
  const { wrapper } = await page(bridge); await button(wrapper, '预览').trigger('click'); await flushPromises();
  await wrapper.get('select[aria-label="重算目标版本"]').setValue('standard-v2'); resolve!({ ok: true, data: { recordCount: 1, oldTotal: '1', newTotal: '2', unpricedCount: 0, incompleteCount: 0, vendors: [], groupName: 'stale', versionDisplayName: 'stale', fingerprint: 'a'.repeat(64) } }); await flushPromises();
  expect(wrapper.find('.recalc-preview').exists()).toBe(false);
});
it('cancelling the confirmation writes nothing and preserves the preview', async () => {
  const { wrapper, bridge } = await page(); const spy = vi.spyOn(bridge, 'call'); await preview(wrapper); await confirm(wrapper);
  await wrapper.findAllComponents({ name: 'ConfirmDialog' })[0]!.findAll('button').find(b => b.text() === '取消')!.trigger('click'); await flushPromises();
  expect(wrapper.find('.recalc-preview').exists()).toBe(true); expect(spy.mock.calls.some(([name]) => name === 'maintenance.commitRecalc')).toBe(false);
});
it('a changed server plan clears confirmation and requires another preview', async () => {
  const { wrapper, bridge } = await page(); const original = bridge.call.bind(bridge); const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'maintenance.commitRecalc' ? { ok: false, error: { code: 'RECALC_PLAN_CHANGED', message: 'changed', retryable: false } } as never : original(name, payload));
  await preview(wrapper); await confirm(wrapper); await wrapper.get('.button--danger').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('请重新预览'); expect(wrapper.find('.recalc-preview').exists()).toBe(false); expect(button(wrapper, '确认重算').attributes('disabled')).toBeDefined();
  expect(spy.mock.calls.filter(([name]) => name === 'maintenance.commitRecalc')).toHaveLength(1);
});
it('timeout after a committed write retries the original fingerprint and operationId', async () => {
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge); let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => { const response = await original(name, payload); return name === 'maintenance.commitRecalc' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : response; });
  const { wrapper } = await page(bridge); await wrapper.get('select[aria-label="重算目标版本"]').setValue('standard-v2'); await preview(wrapper); await confirm(wrapper); await wrapper.get('.button--danger').trigger('click'); await flushPromises();
  const before = unwrap(await bridge.call('wallet.list', {})); expect(wrapper.text()).toContain('操作可能已完成');
  bridge.receiveEvent('wallet.changed', { revision: before.revision, vendorIds: ['vendor-standard'] });
  bridge.receiveEvent('usage.changed', { revision: 2, harnesses: ['codex'] }); await flushPromises();
  expect(wrapper.find('.recalc-preview').exists()).toBe(true); fail = false;
  await wrapper.get('.button--danger').trigger('click'); await flushPromises(); expect(unwrap(await bridge.call('wallet.list', {}))).toEqual(before);
  const writes = spy.mock.calls.filter(([name]) => name === 'maintenance.commitRecalc'); expect(writes[0]![1]).toEqual(writes[1]![1]);
});
it('usage and wallet notifications invalidate the displayed preview', async () => {
  const { wrapper, bridge } = await page(); await preview(wrapper); bridge.receiveEvent('wallet.changed', { revision: 2, vendorIds: ['vendor-standard'] }); await flushPromises(); expect(wrapper.find('.recalc-preview').exists()).toBe(false);
});
it('reversed ranges fail locally without querying the server', async () => {
  const { wrapper, bridge } = await page(); const spy = vi.spyOn(bridge, 'call'); const future = formatDateTimeInput(new Date(Date.now() + 60000), 'UTC');
  wrapper.findAllComponents(DateTimeField)[0]!.vm.$emit('update:modelValue', future); await flushPromises();
  await preview(wrapper); expect(wrapper.text()).toContain('结束时间须晚于开始时间'); expect(spy.mock.calls.some(([name]) => name === 'maintenance.previewRecalc')).toBe(false);
});
it('mock commit updates all statistics breakdowns and rejects stale fingerprints', async () => {
  const { bridge } = await page(); const end = new Date().toISOString(); const start = new Date(Date.now() - 3600000).toISOString();
  const query: RequestMap['stats.summary'] = { harness: 'codex', start, end, offsetMinutes: 0, grain: 'hour' };
  const before = unwrap(await bridge.call('stats.summary', query)); const params = { harness: 'codex' as const, start, end, groupId: 'standard', versionId: 'standard-v2' };
  const plan = unwrap(await bridge.call('maintenance.previewRecalc', params)); unwrap(await bridge.call('maintenance.commitRecalc', { ...params, fingerprint: plan.fingerprint, operationId: crypto.randomUUID() }));
  const after = unwrap(await bridge.call('stats.summary', query)); expect(BigInt(after.metrics.picoUsd)).toBe(BigInt(before.metrics.picoUsd) * 2n);
  expect(after.models[0]!.picoUsd).toBe(after.metrics.picoUsd); expect(after.hourly.reduce((sum, row) => sum + BigInt(row.metrics.picoUsd), 0n)).toBe(BigInt(after.metrics.picoUsd));
  expect(await bridge.call('maintenance.commitRecalc', { ...params, fingerprint: plan.fingerprint, operationId: crypto.randomUUID() })).toMatchObject({ ok: false, error: { code: 'RECALC_PLAN_CHANGED' } });
});
