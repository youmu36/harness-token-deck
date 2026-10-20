import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import MigrationWizard from '../src/features/migration/MigrationWizard.vue';
import App from '../src/App.vue';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import { migrationResult } from '../src/features/migration/types';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); vi.restoreAllMocks(); });
function button(wrapper: VueWrapper, text: string) { return wrapper.findAll('button').find(b => b.text() === text)!; }
async function page(bridge: Bridge = createMockBridge()) {
  const wrapper = mount(MigrationWizard, { global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises(); return { wrapper, bridge };
}
async function rehearse(wrapper: VueWrapper) {
  await button(wrapper, '生成快照并演练').trigger('click'); await flushPromises();
  await vi.advanceTimersByTimeAsync(250); await flushPromises();
}
it('detects, rehearses, shows balances and requires the rollback warning before activation', async () => {
  vi.useFakeTimers(); const { wrapper, bridge } = await page(); const spy = vi.spyOn(bridge, 'call');
  expect(wrapper.text()).toContain('检测到旧版数据库'); await rehearse(wrapper);
  expect(wrapper.text()).toContain('全部校验通过，尚未激活'); expect(wrapper.findAll('tbody tr')).toHaveLength(2);
  expect(wrapper.text()).toContain('6005 → 6005'); expect(wrapper.text()).toContain('警告（1）');
  expect(spy.mock.calls.some(([name]) => name === 'migration.activate')).toBe(false);
  await button(wrapper, '继续').trigger('click');
  expect(wrapper.text()).toContain('新版数据不会同步回旧版');
  await button(wrapper, '确认激活').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('旧版数据已激活'); expect(wrapper.text()).toContain('旧库保留位置');
  expect(wrapper.emitted('activated')).toHaveLength(1);
});
it('blocks a running legacy app and a populated target while keeping detection retry available', async () => {
  const options = { legacyRunning: true, targetEmpty: true }; const { wrapper } = await page(createMockBridge({ migration: options }));
  expect(wrapper.text()).toContain('请先退出旧版'); expect(button(wrapper, '生成快照并演练').attributes('disabled')).toBeDefined();
  options.legacyRunning = false; options.targetEmpty = false; await button(wrapper, '重新检测').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('新版已有账本或钱包凭证');
  options.targetEmpty = true; await button(wrapper, '重新检测').trigger('click'); await flushPromises();
  expect(button(wrapper, '生成快照并演练').attributes('disabled')).toBeUndefined();
});
it('failure displays the report path and offers another rehearsal without activation', async () => {
  vi.useFakeTimers(); const { wrapper } = await page(createMockBridge({ migration: { fail: true } })); await rehearse(wrapper);
  expect(wrapper.text()).toContain('迁移校验失败，不能激活'); expect(wrapper.text()).toContain('report.md');
  expect(button(wrapper, '重新演练').exists()).toBe(true); expect(wrapper.findAll('button').some(b => b.text() === '继续')).toBe(false);
});
it('uses a typed custom source for detection, rehearsal and the final source location', async () => {
  vi.useFakeTimers(); const { wrapper, bridge } = await page(); const spy = vi.spyOn(bridge, 'call');
  const selected = 'D:\\用户备份\\old-ledger.sqlite3';
  await wrapper.get('#legacy-source-path').setValue(selected); await flushPromises();
  expect(spy).toHaveBeenCalledWith('migration.detect', { sourcePath: selected });
  await rehearse(wrapper);
  expect(spy.mock.calls.find(([name]) => name === 'migration.run')?.[1]).toMatchObject({ sourcePath: selected });
  await button(wrapper, '继续').trigger('click'); await button(wrapper, '确认激活').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain(selected);
});
it('selects a file, preserves the path when cancelled, and returns to source selection after failure', async () => {
  vi.useFakeTimers(); const bridge = createMockBridge({ migration: { fail: true } }); const original = bridge.call.bind(bridge);
  let selected: string | null = 'E:\\Backups\\legacy.db';
  vi.spyOn(bridge, 'call').mockImplementation((name, payload) => name === 'app.pickPath' ? Promise.resolve({ ok: true, data: { path: selected } }) as never : original(name, payload));
  const { wrapper } = await page(bridge);
  await button(wrapper, '选择文件…').trigger('click'); await flushPromises();
  expect((wrapper.get('#legacy-source-path').element as HTMLInputElement).value).toBe(selected);
  selected = null; await button(wrapper, '选择文件…').trigger('click'); await flushPromises();
  expect((wrapper.get('#legacy-source-path').element as HTMLInputElement).value).toBe('E:\\Backups\\legacy.db');
  await rehearse(wrapper); await button(wrapper, '更换旧库').trigger('click'); await flushPromises();
  expect(wrapper.find('#legacy-source-path').exists()).toBe(true); expect(wrapper.text()).not.toContain('报告路径：');
  await wrapper.get('#legacy-source-path').setValue(''); await flushPromises();
  expect(bridge.call).toHaveBeenCalledWith('migration.detect', {});
});
it('disables rehearsal while a changed source is still being detected', async () => {
  const { wrapper, bridge } = await page(); const original = bridge.call.bind(bridge);
  let complete!: (value: unknown) => void;
  vi.spyOn(bridge, 'call').mockImplementation((name, payload) => name === 'migration.detect' ? new Promise(resolve => { complete = resolve; }) as never : original(name, payload));
  await wrapper.get('#legacy-source-path').setValue('D:\\missing.sqlite3'); await flushPromises();
  expect(button(wrapper, '生成快照并演练').attributes('disabled')).toBeDefined();
  complete({ ok: true, data: { detected: false, sourcePath: 'D:\\missing.sqlite3', legacyRunning: false, targetEmpty: true, canMigrate: false, reason: '未发现旧版数据库' } });
  await flushPromises(); expect(button(wrapper, '生成快照并演练').attributes('disabled')).toBeDefined();
});
it('retries an activation timeout with the original request and recovers the committed result', async () => {
  vi.useFakeTimers(); const { wrapper, bridge } = await page(); await rehearse(wrapper); await button(wrapper, '继续').trigger('click');
  const original = bridge.call.bind(bridge); let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const response = await original(name, payload);
    return name === 'migration.activate' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : response;
  });
  await button(wrapper, '确认激活').trigger('click'); await flushPromises(); expect(wrapper.text()).toContain('操作可能已完成');
  fail = false; await button(wrapper, '确认激活').trigger('click'); await flushPromises();
  const writes = spy.mock.calls.filter(([name]) => name === 'migration.activate'); expect(writes[0]![1]).toEqual(writes[1]![1]);
  expect(wrapper.text()).toContain('旧版数据已激活');
});
it('retries a run timeout with its original operation and resumes the same hidden task', async () => {
  vi.useFakeTimers(); const bridge = createMockBridge(); const original = bridge.call.bind(bridge); let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const response = await original(name, payload);
    return name === 'migration.run' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : response;
  });
  const { wrapper } = await page(bridge); await button(wrapper, '生成快照并演练').trigger('click'); await flushPromises(); fail = false;
  await button(wrapper, '生成快照并演练').trigger('click'); await flushPromises();
  const writes = spy.mock.calls.filter(([name]) => name === 'migration.run'); expect(writes[0]![1]).toEqual(writes[1]![1]);
  bridge.receiveEvent('app.visibility', { visible: false }); const reads = spy.mock.calls.filter(([name]) => name === 'tasks.get').length;
  await vi.advanceTimersByTimeAsync(1500); await flushPromises(); expect(spy.mock.calls.filter(([name]) => name === 'tasks.get')).toHaveLength(reads);
  bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises(); expect(wrapper.text()).toContain('全部校验通过');
});
it('cancelled rehearsals cannot proceed to activation', async () => {
  vi.useFakeTimers(); const { wrapper } = await page(); await button(wrapper, '生成快照并演练').trigger('click'); await flushPromises();
  await button(wrapper, '取消演练').trigger('click'); await flushPromises(); await vi.advanceTimersByTimeAsync(250); await flushPromises();
  expect(wrapper.text()).toContain('演练已取消'); expect(wrapper.text()).toContain('report.md');
  expect(wrapper.findAll('button').some(b => b.text() === '继续')).toBe(false);
});
it('first launch retains legacy migration after the setup guide and settings keeps the entry', async () => {
  history.replaceState(null, '', '/'); const bridge = createMockBridge();
  const wrapper = mount(App, { global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  expect(wrapper.text()).toContain('打开使用指南');
  await button(wrapper, '打开使用指南').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('欢迎使用 · 简要使用指南');
  await button(wrapper, '稍后设置').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('检测到旧版数据'); expect(wrapper.findComponent(MigrationWizard).exists()).toBe(false);
  await button(wrapper, '导入旧版数据…').trigger('click'); await flushPromises(); expect(wrapper.findComponent(MigrationWizard).exists()).toBe(true);
  await button(wrapper, '关闭').trigger('click'); await flushPromises();
  await wrapper.get('nav a[aria-label="设置"]').trigger('click'); await flushPromises();
  expect(wrapper.findAll('button').some(b => b.text() === '导入旧版数据…')).toBe(true);
});
it('rejects malformed task summaries before showing money or activation', () => {
  expect(migrationResult({ ok: true, migrationId: 'id', sourcePath: 'old', snapshotPath: 'copy', reportPath: 'report', oldTotal: '1.1', newTotal: '0', warnings: [], errors: [], counts: [], vendors: [] })).toBeUndefined();
});

it('dismisses the legacy banner persistently while settings keeps manual import', async () => {
  history.replaceState(null, '', '/'); const bridge = createMockBridge();
  const wrapper = mount(App, { global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); }); await flushPromises();
  await wrapper.get('[aria-label="关闭旧版数据提示"]').trigger('click'); await flushPromises();
  expect(wrapper.text()).not.toContain('检测到旧版数据。');
  const saved = await bridge.call('preferences.get', {});
  expect(saved.ok && saved.data.preferences.migrationHintDismissed).toBe(true);
  await wrapper.get('nav a[aria-label="设置"]').trigger('click'); await flushPromises();
  await button(wrapper, '数据与历史').trigger('click'); await flushPromises();
  expect(wrapper.findAll('button').some(b => b.text() === '导入旧版数据…')).toBe(true);
});
