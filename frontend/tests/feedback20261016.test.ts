import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, test, vi } from 'vitest';
import App from '../src/App.vue';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import { entryNote } from '../src/features/wallet/entryNotes';
import ReconciliationNotice from '../src/features/reconciliation/ReconciliationNotice.vue';
import RemoteAccountDialog from '../src/features/wallet/RemoteAccountDialog.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach(fn => fn()); history.replaceState(null, '', '/'); document.body.innerHTML = ''; vi.restoreAllMocks(); });
async function readyBridge(patch: object = {}) {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  unwrap(await bridge.call('preferences.update', { operationId: crypto.randomUUID(), expectedRevision: 0, patch: { guideSeen: true, historyPrompted: true, ...patch } }));
  return bridge;
}
async function render(bridge: Bridge) {
  const wrapper = mount(App, { global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises(); return wrapper;
}

test('hidden pages stay inaccessible after restart and every Harness uses the shared page logic', async () => {
  history.replaceState(null, '', '/');
  const bridge = await readyBridge({ hiddenHarnesses: ['claude'], lastRoute: '/harness/claude', lastPage: 'claude' });
  const wrapper = await render(bridge);
  expect(wrapper.get('h1').text()).toBe('数据统计与分析');
  expect(wrapper.find('nav a[aria-label="Claude Code"]').exists()).toBe(false);
  location.hash = '#/harness/claude'; window.dispatchEvent(new HashChangeEvent('hashchange')); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('数据统计与分析');
  const preferences = unwrap(await bridge.call('preferences.get', {}));
  expect(preferences.preferences.lastRoute).toBe('/overview');
  await wrapper.get('nav a[aria-label="设置"]').trigger('click'); await flushPromises();
  await wrapper.get('input[aria-label="显示 Claude Code 页面"]').setValue(true); await flushPromises();
  expect(wrapper.find('nav a[aria-label="Claude Code"]').exists()).toBe(true);
});

test('settings mount only the selected category and remember it on return', async () => {
  history.replaceState(null, '', '/');
  const bridge = await readyBridge({ lastRoute: '/settings', lastPage: 'settings' });
  const spy = vi.spyOn(bridge, 'call'); const wrapper = await render(bridge);
  expect(spy.mock.calls.some(([name]) => name === 'sources.list')).toBe(false);
  expect(spy.mock.calls.some(([name]) => name === 'maintenance.status')).toBe(false);
  await wrapper.findAll('nav[aria-label="设置分类"] button').find(b => b.text() === '采集来源')!.trigger('click'); await flushPromises();
  expect(spy.mock.calls.some(([name]) => name === 'sources.list')).toBe(true);
  await wrapper.get('nav a[aria-label="Codex"]').trigger('click'); await flushPromises();
  await wrapper.get('nav a[aria-label="设置"]').trigger('click'); await flushPromises();
  expect(wrapper.get('nav[aria-label="设置分类"] button[aria-current="page"]').text()).toBe('采集来源');
  expect(wrapper.find('input[aria-label="自动匹配供应商（Beta）"]').exists()).toBe(false);
});

test('route is saved after rapid committed navigation and shared preference listeners read once', async () => {
  history.replaceState(null, '', '/'); const bridge = await readyBridge({ lastRoute: '/harness/codex' });
  const wrapper = await render(bridge); const spy = vi.spyOn(bridge, 'call');
  bridge.receiveEvent('preferences.changed', { revision: 999 }); await flushPromises();
  expect(spy.mock.calls.filter(([name]) => name === 'preferences.get')).toHaveLength(1);
  await wrapper.get('nav a[aria-label="Claude Code"]').trigger('click');
  await wrapper.get('nav a[aria-label="DSH"]').trigger('click');
  await wrapper.get('nav a[aria-label="供应商与钱包"]').trigger('click'); await flushPromises();
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.lastRoute).toBe('/wallet');
});

test('calibration notices show actual supplier mismatch and disappear on resolved snapshots', async () => {
  const bridge = await readyBridge(); const original = bridge.call.bind(bridge); let mismatch = true;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'reconciliation.status' ? { ok: true, data: { notices: mismatch ? [{ vendorId: 'v', vendorName: '实际供应商', harness: 'codex', selectedGroupId: 'g', selectedGroupName: '其他供应商组', observedAt: '2026-10-16T00:00:00.000Z', remoteDecreasePico: '123' }] : [] } } as never : original(name, payload));
  const wrapper = mount(ReconciliationNotice, { props: { harness: 'codex' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.text()).toContain('实际供应商'); expect(wrapper.get('a').attributes('href')).toBe('#/harness/codex');
  mismatch = false; bridge.receiveEvent('pricing.changed', { revision: 2 }); await flushPromises(); expect(wrapper.text()).toBe('');
});

test('DeepSeek uses vendor website and API key without a New API user ID', async () => {
  const bridge = await readyBridge();
  const wrapper = mount(RemoteAccountDialog, { props: { vendorId: 'vendor-standard', vendorName: 'DeepSeek', vendorUrl: 'https://api.deepseek.com' }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect((wrapper.get('input[aria-label="站点地址"]').element as HTMLInputElement).value).toBe('https://api.deepseek.com');
  await wrapper.get('select[aria-label="接口类型"]').setValue('deepseek');
  expect(wrapper.find('input[aria-label="用户 ID"]').exists()).toBe(false);
  await wrapper.get('input[aria-label="令牌"]').setValue('synthetic-deepseek-key');
  await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('连接成功');
});

test('daily spending and calibration notes preserve original amounts', () => {
  const entry = { id: null, kind: 'consumption_daily' as const, seq: 1, amountPico: '0', occurredAt: '', count: 1, note: 'original_spending', calibration: null, noteDetails: null };
  expect(entryNote(entry)).toContain('原始估算消费');
  expect(entryNote({ ...entry, note: 'original_spending_corrected' })).toContain('原消费金额保留');
  expect(entryNote({ ...entry, kind: 'adjustment', note: 'legacy_compacted' })).toContain('无法拆分');
  expect(entryNote({ ...entry, kind: 'adjustment', note: 'migration' })).toBe('旧版数据迁移修正');
});
