import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import Display from '../src/features/settings/Display.vue';
import Sources from '../src/features/settings/Sources.vue';
import SourceCard from '../src/features/settings/SourceCard.vue';
import Background from '../src/features/settings/Background.vue';
import Data from '../src/features/settings/Data.vue';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); vi.useRealTimers(); });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
const button = (wrapper: ReturnType<typeof mount>, text: string) => wrapper.findAll('button').find(b => b.text() === text)!;
async function data(bridge = mock()) { const wrapper = mount(Data, { global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises(); return { wrapper, bridge }; }
it('keeps a compact disk summary without refreshing it on unrelated events or a timer', async () => {
  vi.useFakeTimers();
  const bridge = mock(); const spy = vi.spyOn(bridge, 'call');
  const { wrapper } = await data(bridge);
  const statusReads = () => spy.mock.calls.filter(([name]) => name === 'maintenance.status').length;
  expect(statusReads()).toBe(1);
  expect(wrapper.get('.storage-details').findAll('p')).toHaveLength(2);
  expect(wrapper.get('.storage-details').text()).toContain('总占用：');
  expect(wrapper.get('.storage-details').text()).toContain('备份占用：');
  expect(wrapper.get('.storage-details').text()).not.toMatch(/WAL|SHM|逻辑|内存|采样时间/);
  bridge.receiveEvent('sources.changed', { revision: 2 });
  bridge.receiveEvent('preferences.changed', { revision: 3 });
  bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 4 });
  await vi.advanceTimersByTimeAsync(10 * 60_000); await flushPromises();
  expect(statusReads()).toBe(1);
  await button(wrapper, '备份数据库').trigger('click'); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  expect(statusReads()).toBe(2);
});
it('display changes roll back every control on failed saves and synchronize after successful retry', async () => {
  const bridge = mock(); const initial = unwrap(await bridge.call('preferences.get', {})); const original = bridge.call.bind(bridge); let fail = true;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'preferences.update' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : original(name, payload));
  const wrapper = mount(Display, { props: { initial }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount());
  for (const [label, value, previous] of [['显示主题', 'dark', 'system'], ['统计时区', 'UTC', 'system']]) {
    await wrapper.get(`select[aria-label="${label}"]`).setValue(value); await flushPromises(); expect((wrapper.get(`select[aria-label="${label}"]`).element as HTMLSelectElement).value).toBe(previous);
  }
  await wrapper.get('input[type="checkbox"]').setValue(true); await flushPromises(); expect((wrapper.get('input').element as HTMLInputElement).checked).toBe(false); expect(wrapper.text()).toContain('未能保存扫描倒计时');
  fail = false; await wrapper.get('select[aria-label="统计时区"]').setValue('UTC'); await flushPromises(); expect(unwrap(await bridge.call('preferences.get', {})).preferences.timezone).toBe('UTC');
});
it('source validation reports unavailable paths, saving preserves the collection start, and failed saves retain the draft', async () => {
  vi.useFakeTimers(); const bridge = mock(); const wrapper = mount(Sources, { global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  const before = unwrap(await bridge.call('sources.list', {})); const card = wrapper.findAllComponents(SourceCard)[0]!;
  await card.get('input').setValue('C:\\missing\\sessions'); await vi.advanceTimersByTimeAsync(250); await flushPromises(); expect(card.text()).toContain('路径不可用');
  await button(card as unknown as ReturnType<typeof mount>, '保存').trigger('click'); await flushPromises(); const source = unwrap(await bridge.call('sources.list', {})).sources[0]!;
  expect(source.path).toBe('C:\\missing\\sessions'); expect(source.startedAt).toBe(before.sources[0]!.startedAt); expect(source.status).toBe('unavailable');
  await card.get('input').setValue('relative-path'); await vi.advanceTimersByTimeAsync(250); await button(card as unknown as ReturnType<typeof mount>, '保存').trigger('click'); await flushPromises(); expect((card.get('input').element as HTMLInputElement).value).toBe('relative-path');
});
it('native browsing requests the correct file/directory kind, cancellation preserves input, and choosing only changes the draft', async () => {
  const bridge = mock(); const original = bridge.call.bind(bridge); let path: string | null = null;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'app.pickPath' ? { ok: true, data: { path } } as never : original(name, payload));
  const wrapper = mount(Sources, { global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  const cards = wrapper.findAllComponents(SourceCard); const initial = (cards[1]!.get('input').element as HTMLInputElement).value;
  await button(cards[1] as unknown as ReturnType<typeof mount>, '浏览…').trigger('click'); await flushPromises(); expect((cards[1]!.get('input').element as HTMLInputElement).value).toBe(initial);
  path = 'C:\\Preview\\another.db'; await button(cards[1] as unknown as ReturnType<typeof mount>, '浏览…').trigger('click'); await flushPromises();
  expect(spy.mock.calls.filter(([name]) => name === 'app.pickPath')[1]![1]).toMatchObject({ kind: 'file' }); expect(unwrap(await bridge.call('sources.list', {})).sources[1]!.path).toBe(initial);
  await button(cards[0] as unknown as ReturnType<typeof mount>, '浏览…').trigger('click'); await flushPromises(); expect(spy.mock.calls.filter(([name]) => name === 'app.pickPath').at(-1)![1]).toMatchObject({ kind: 'dir' });
});
it('pausing and resuming reflect server state, including external tray changes', async () => {
  const bridge = mock(); const wrapper = mount(Background, { props: { initial: unwrap(await bridge.call('preferences.get', {})) }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  await button(wrapper, '暂停采集').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('采集已暂停');
  unwrap(await bridge.call('collection.setPaused', { paused: false, operationId: crypto.randomUUID() })); await flushPromises(); expect(wrapper.text()).toContain('采集运行中');
});
it('shortening retention previews both ledger and inbox; cancellation performs no writes and restores the control', async () => {
  const { wrapper, bridge } = await data(); const spy = vi.spyOn(bridge, 'call'); await wrapper.get('select').setValue('90'); await flushPromises();
  expect(wrapper.get('dialog[open]').text()).toContain('12 条到期用量发布为永久日汇总'); expect(spy.mock.calls.some(([name]) => name === 'maintenance.setRetention')).toBe(false);
  await wrapper.get('dialog[open] .button--secondary').trigger('click'); await flushPromises(); expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('360');
  await wrapper.get('select').setValue('90'); await flushPromises(); await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises(); expect(unwrap(await bridge.call('maintenance.status', {})).retention).toBe(90);
});
it('retention conflicts close the stale plan and require a fresh preview', async () => {
  const { wrapper, bridge } = await data(); await wrapper.get('select').setValue('90'); await flushPromises();
  const prefs = unwrap(await bridge.call('preferences.get', {})); unwrap(await bridge.call('preferences.update', { patch: { theme: 'dark' }, expectedRevision: prefs.revision, operationId: crypto.randomUUID() })); await flushPromises();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('重新选择期限并预览确认'); expect(wrapper.find('dialog[open]').exists()).toBe(false);
  expect(unwrap(await bridge.call('maintenance.status', {})).retention).toBe(360);
});
it('partial retention continues with the same operationId and original revision, then settles', async () => {
  const bridge = mock(); const original = bridge.call.bind(bridge); let partial = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => { const response = await original(name, payload); if (name === 'maintenance.setRetention' && partial && response.ok) { partial = false; return { ...response, data: { ...response.data, complete: false, errorCode: 'RETENTION_FAILED' } } as never; } return response; });
  const { wrapper } = await data(bridge); await wrapper.get('select').setValue('90'); await flushPromises(); await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('清理尚未完成'); await button(wrapper, '继续清理').trigger('click'); await flushPromises();
  const writes = spy.mock.calls.filter(([name]) => name === 'maintenance.setRetention'); expect(writes[0]![1]).toEqual(writes[1]![1]); expect(wrapper.text()).not.toContain('清理尚未完成');
});
it('retention save failure rolls back the control and an explicit retry keeps the original request', async () => {
  const bridge = mock(); const original = bridge.call.bind(bridge); let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'maintenance.setRetention' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : original(name, payload));
  const { wrapper } = await data(bridge); await wrapper.get('select').setValue('forever'); await flushPromises();
  expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('360'); fail = false;
  await button(wrapper, '重试保存与清理').trigger('click'); await flushPromises(); const writes = spy.mock.calls.filter(([name]) => name === 'maintenance.setRetention');
  expect(writes[0]![1]).toEqual(writes[1]![1]); expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('forever');
});
it('source path validation ignores a late response for an older draft', async () => {
  vi.useFakeTimers(); const bridge = mock(); const original = bridge.call.bind(bridge); let resolveOld: ((value: unknown) => void) | undefined;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'sources.validatePath' && 'path' in payload && payload.path === 'C:\\slow' ? await new Promise<unknown>(resolve => { resolveOld = resolve; }) as never : original(name, payload));
  const wrapper = mount(Sources, { global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises(); const card = wrapper.findAllComponents(SourceCard)[0]!;
  await card.get('input').setValue('C:\\slow'); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  await card.get('input').setValue('C:\\missing'); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  resolveOld!({ ok: true, data: { harness: 'codex', path: 'C:\\slow', available: true, diagnostics: [] } }); await flushPromises();
  expect(card.text()).toContain('路径不可用'); expect(card.text()).not.toContain('路径存在');
});
it('backup and vacuum use task completion and show the actual returned backup path', async () => {
  vi.useFakeTimers(); const { wrapper } = await data(); await button(wrapper, '备份数据库').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('正在备份'); await vi.advanceTimersByTimeAsync(250); await flushPromises(); expect(wrapper.text()).toContain('backups\\example.sqlite3');
  await button(wrapper, '回收空闲空间').trigger('click'); await flushPromises(); await vi.advanceTimersByTimeAsync(250); await flushPromises(); expect(wrapper.text()).toContain('空闲空间已回收');
});

it('source scan switches preserve paths, collection starts and navigation visibility', async () => {
  const bridge = mock(); const before = unwrap(await bridge.call('sources.list', {}));
  const wrapper = mount(Sources, { global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  const card = wrapper.findAllComponents(SourceCard)[0]!;
  await card.get('input[type="checkbox"]').setValue(false); await flushPromises();
  let current = unwrap(await bridge.call('sources.list', {})).sources[0]!;
  expect(current.scanEnabled).toBe(false); expect(current.path).toBe(before.sources[0]!.path); expect(current.startedAt).toBe(before.sources[0]!.startedAt);
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.hiddenHarnesses).toEqual([]);
  await card.get('input[type="checkbox"]').setValue(true); await flushPromises();
  current = unwrap(await bridge.call('sources.list', {})).sources[0]!; expect(current.scanEnabled).toBe(true); expect(current.startedAt).toBe(before.sources[0]!.startedAt);
});
