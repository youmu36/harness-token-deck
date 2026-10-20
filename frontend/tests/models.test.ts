import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, NavigationGuardKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import ModelsTab from '../src/features/models/ModelsTab.vue';
import ModelScanDialog from '../src/features/models/ModelScanDialog.vue';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); vi.useRealTimers(); });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
const button = (wrapper: ReturnType<typeof mount>, label: string) => wrapper.findAll('button').find(b => b.text() === label)!;
async function page(bridge = mock()) { const wrapper = mount(ModelsTab, { attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises(); return { wrapper, bridge }; }
it('highlights dirty rows, saves several precise fields together, and leaves price history intact', async () => {
  const { wrapper, bridge } = await page(); const catalog = unwrap(await bridge.call('pricing.catalog', {}));
  for (const [field, value] of [['输入价格', '0'], ['缓存命中价格', '0.000001'], ['缓存创建价格', '3.75'], ['输出价格', '2.000000']]) {
    const input = wrapper.get(`input[aria-label="gpt-6 ${field}"]`).element as HTMLInputElement; input.value = value!; input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  await flushPromises(); expect(wrapper.findAll('tr.dirty')).toHaveLength(1); expect(wrapper.text()).toContain('未保存 1 行');
  await button(wrapper, '保存全部修改').trigger('click'); await flushPromises();
  expect(unwrap(await bridge.call('models.pool', {})).models[0]!.rates).toEqual({ input: '0', cache: '0.000001', cacheWrite: '3.75', output: '2' });
  expect(unwrap(await bridge.call('pricing.catalog', {})).groups).toEqual(catalog.groups); expect(wrapper.findAll('tr.dirty')).toHaveLength(0);
});
it('preserves failed drafts and reuses the operationId for an unchanged retry', async () => {
  const { wrapper, bridge } = await page(); const original = bridge.call.bind(bridge); let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'models.saveBaseRates' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : original(name, payload));
  await wrapper.get('input[aria-label="gpt-6 输入价格"]').setValue('4.5'); await button(wrapper, '保存全部修改').trigger('click'); await flushPromises();
  expect((wrapper.get('input[aria-label="gpt-6 输入价格"]').element as HTMLInputElement).value).toBe('4.5');
  fail = false; await button(wrapper, '保存全部修改').trigger('click'); await flushPromises();
  const writes = spy.mock.calls.filter(([name]) => name === 'models.saveBaseRates'); expect(writes[0]![1]).toEqual(writes[1]![1]);
});
it('conflicts refresh the shared revision, preserve drafts, and require another save', async () => {
  const { wrapper, bridge } = await page(); await wrapper.get('input[aria-label="gpt-6 输入价格"]').setValue('9');
  const pool = unwrap(await bridge.call('models.pool', {})); unwrap(await bridge.call('models.add', { modelIds: ['remote-model'], expectedRevision: pool.revision, operationId: crypto.randomUUID() })); await flushPromises();
  await button(wrapper, '保存全部修改').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('保留草稿');
  expect(unwrap(await bridge.call('models.pool', {})).models[0]!.rates.input).toBe('1.5');
  await button(wrapper, '保存全部修改').trigger('click'); await flushPromises(); expect(unwrap(await bridge.call('models.pool', {})).models[0]!.rates.input).toBe('9');
});
it('a remotely removed model keeps its unsaved draft until the user confirms discard', async () => {
  const { wrapper, bridge } = await page(); await wrapper.get('input[aria-label="gpt-6 输入价格"]').setValue('9');
  const pool = unwrap(await bridge.call('models.pool', {})); unwrap(await bridge.call('models.remove', { modelId: 'gpt-6', expectedRevision: pool.revision, operationId: crypto.randomUUID() })); await flushPromises();
  expect(wrapper.text()).toContain('模型已被移出，草稿保留'); expect((wrapper.get('input[aria-label="gpt-6 输入价格"]').element as HTMLInputElement).value).toBe('9');
  expect(button(wrapper, '保存全部修改').attributes('disabled')).toBeDefined(); await button(wrapper, '放弃修改').trigger('click'); await flushPromises();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises(); expect(wrapper.find('input[aria-label="gpt-6 输入价格"]').exists()).toBe(false);
});
it('removal needs confirmation, excludes automatic sync, and restoration starts with empty base rates', async () => {
  const { wrapper, bridge } = await page(); const spy = vi.spyOn(bridge, 'call'); await wrapper.get('input[type=search]').setValue('gpt-6'); await button(wrapper, '移出').trigger('click'); await flushPromises();
  expect(spy.mock.calls.some(([name]) => name === 'models.remove')).toBe(false); await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises();
  let pool = unwrap(await bridge.call('models.pool', {})); expect(pool.excluded).toContain('gpt-6'); expect(pool.models.some(m => m.modelId === 'gpt-6')).toBe(false);
  await button(wrapper, '恢复').trigger('click'); await flushPromises(); pool = unwrap(await bridge.call('models.pool', {}));
  expect(pool.excluded).not.toContain('gpt-6'); expect(pool.models.find(m => m.modelId === 'gpt-6')!.rates).toEqual({ input: null, cache: null, output: null, cacheWrite: null });
});
it('scan marks excluded models, defaults to new non-excluded candidates, and adds only on confirmation', async () => {
  vi.useFakeTimers(); const bridge = mock(); const pool = unwrap(await bridge.call('models.pool', {}));
  const removed = unwrap(await bridge.call('models.remove', { modelId: 'gpt-6', expectedRevision: pool.revision, operationId: crypto.randomUUID() }));
  const wrapper = mount(ModelScanDialog, { props: { revision: removed.revision, refresh: async () => {} }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.text()).toContain('已读取 0 个文件'); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  expect(wrapper.text()).toContain('被移出过'); const check = wrapper.findAll('input[value="gpt-6"]')[0]!; expect((check.element as HTMLInputElement).checked).toBe(false);
  expect(unwrap(await bridge.call('models.pool', {})).models.some(m => m.modelId === 'new-source-model')).toBe(false);
  await check.setValue(true); await button(wrapper, '加入选中模型（2）').trigger('click'); await flushPromises();
  const saved = unwrap(await bridge.call('models.pool', {})); expect(saved.excluded).not.toContain('gpt-6'); expect(saved.models.some(m => m.modelId === 'new-source-model')).toBe(true);
});
it('scan cancellation settles the task without adding models and permits a fresh scan', async () => {
  vi.useFakeTimers(); const bridge = mock(); const pool = unwrap(await bridge.call('models.pool', {}));
  const wrapper = mount(ModelScanDialog, { props: { revision: pool.revision, refresh: async () => {} }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  await button(wrapper, '取消扫描').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('正在取消'); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  expect(wrapper.text()).toContain('扫描已取消'); expect(unwrap(await bridge.call('models.pool', {}))).toEqual(pool);
  await button(wrapper, '重新扫描').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('已读取');
});
it('hidden scanning stops reads and restores the completed task without starting another scan', async () => {
  vi.useFakeTimers(); const bridge = mock(); const pool = unwrap(await bridge.call('models.pool', {})); const spy = vi.spyOn(bridge, 'call');
  const wrapper = mount(ModelScanDialog, { props: { revision: pool.revision, refresh: async () => {} }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  bridge.receiveEvent('app.visibility', { visible: false }); const reads = spy.mock.calls.filter(([name]) => name === 'tasks.get').length;
  await vi.advanceTimersByTimeAsync(1250); await flushPromises(); expect(spy.mock.calls.filter(([name]) => name === 'tasks.get')).toHaveLength(reads);
  bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises(); expect(wrapper.text()).toContain('new-source-model');
  expect(spy.mock.calls.filter(([name]) => name === 'models.scan')).toHaveLength(1);
});
it('search filters rows and dirty navigation requires explicit discard', async () => {
  const bridge = mock(); let guard: ((proceed: () => void) => void) | undefined;
  const wrapper = mount(ModelsTab, { global: { provide: { [BridgeKey as symbol]: bridge, [NavigationGuardKey as symbol]: (value: typeof guard) => { guard = value; return () => {}; } }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  await wrapper.get('input[type="search"]').setValue('gpt'); expect(wrapper.findAll('tbody tr')).toHaveLength(1);
  await wrapper.get('input[aria-label="gpt-6 输入价格"]').setValue('7'); const proceed = vi.fn(); guard!(proceed); await flushPromises(); expect(proceed).not.toHaveBeenCalled();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); expect(proceed).toHaveBeenCalledOnce();
});
