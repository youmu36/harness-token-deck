import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import App from '../src/App.vue';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { createStartupBridge } from '../src/app/startup';
import { unwrap } from '../src/infra/bridge';
import { createMockBridge as createUnpromptedBridge } from '../src/infra/bridge/mock';
function createMockBridge() {
  const bridge = createUnpromptedBridge();
  void bridge.call('preferences.update', { expectedRevision: 0, operationId: crypto.randomUUID(), patch: { historyPrompted: true, guideSeen: true, lastRoute: '/harness/codex' } });
  return bridge;
}
const cleanup: (() => void)[] = [];
beforeEach(() => { history.replaceState(null, '', '/'); });
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); });
async function render(bridge: Bridge = createMockBridge()) {
  const wrapper = mount(App, { global: { provide: { [BridgeKey as symbol]: bridge } }, attachTo: document.body });
  cleanup.push(() => { wrapper.unmount(); bridge.dispose(); });
  await flushPromises(); return wrapper;
}

test('skip link requires fresh keyboard navigation and stays hidden after tray or window focus restore', async () => {
  const bridge = createMockBridge(); const wrapper = await render(bridge);
  const link = wrapper.get<HTMLAnchorElement>('.skip-link');
  link.element.focus(); await flushPromises();
  expect(link.classes()).not.toContain('keyboard-focus');
  wrapper.get<HTMLElement>('main').element.focus();
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  link.element.focus(); await flushPromises();
  expect(link.classes()).toContain('keyboard-focus');
  window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus')); await flushPromises();
  expect(link.classes()).not.toContain('keyboard-focus');
  wrapper.get<HTMLElement>('main').element.focus();
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  link.element.focus(); await flushPromises(); expect(link.classes()).toContain('keyboard-focus');
  bridge.receiveEvent('app.visibility', { visible: false }); await flushPromises();
  bridge.receiveEvent('app.visibility', { visible: true }); await flushPromises();
  expect(link.classes()).not.toContain('keyboard-focus');
  wrapper.get<HTMLElement>('main').element.focus();
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  link.element.focus(); await flushPromises();
  const hash = window.location.hash;
  await link.trigger('click'); await flushPromises();
  expect(document.activeElement).toBe(wrapper.get('main').element);
  expect(window.location.hash).toBe(hash); expect(link.classes()).not.toContain('keyboard-focus');
});
test('dirty prices guard both hash navigation and sidebar navigation until confirmed', async () => {
  const wrapper = await render();
  await wrapper.get('nav a[aria-label="价格与模型"]').trigger('click'); await flushPromises();
  await wrapper.get('[aria-label="价格组二级导航"] button:nth-child(2)').trigger('click'); await flushPromises();
  await wrapper.get('input[aria-label="输入价格"]').setValue('2.5');
  window.location.hash = '#/wallet'; window.dispatchEvent(new HashChangeEvent('hashchange')); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('价格与模型'); expect(window.location.hash).toBe('#/pricing');
  let dialog = document.querySelector('dialog[open]')!;
  (dialog.querySelector('.button--secondary') as HTMLButtonElement).click(); await flushPromises();
  expect((wrapper.get('input[aria-label="输入价格"]').element as HTMLInputElement).value).toBe('2.5');
  await wrapper.get('nav a[aria-label="供应商与钱包"]').trigger('click'); await flushPromises();
  dialog = document.querySelector('dialog[open]')!;
  (dialog.querySelector('.button--danger') as HTMLButtonElement).click(); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('供应商与钱包'); expect(window.location.hash).toBe('#/wallet');
});
test('startup opens Codex; successful navigation across eight pages persists each destination', async () => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const wrapper = await render(bridge);
  expect(wrapper.get('h1').text()).toBe('Codex 统计');
  expect(wrapper.find('.toolbar').exists()).toBe(false);
  expect(wrapper.find('select[aria-label="主题"]').exists()).toBe(false);
  expect(wrapper.get('.environment').text()).toContain('浏览器预览');
  for (const [label, title] of [['Claude Code', 'Claude Code 统计'], ['数据统计与分析', '数据统计与分析'], ['Zcode', 'Zcode 统计'], ['DSH', 'Deepseek Harness 统计'], ['供应商与钱包', '供应商与钱包'], ['价格与模型', '价格与模型'], ['设置', '设置'], ['Codex', 'Codex 统计']]) {
    await wrapper.get(`nav a[aria-label="${label}"]`).trigger('click', { button: 0 });
    await flushPromises(); expect(wrapper.get('h1').text()).toBe(title);
  }
  expect(call.mock.calls.filter(([name]) => name === 'preferences.update')).toHaveLength(8);
});
test('lastPage is saved during navigation and restored at the next startup; hiding is idempotent', async () => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  const wrapper = await render(bridge);
  await wrapper.get('nav a[aria-label="设置"]').trigger('click', { button: 0 });
  await flushPromises(); expect(call.mock.calls.filter(([name]) => name === 'preferences.update')).toHaveLength(1);
  bridge.receiveEvent('app.visibility', { visible: false }); await flushPromises();
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.lastPage).toBe('settings');
  expect(call.mock.calls.filter(([name]) => name === 'preferences.update')).toHaveLength(1);
  bridge.receiveEvent('app.visibility', { visible: false }); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'preferences.update')).toHaveLength(1);
  wrapper.unmount(); history.replaceState(null, '', '/');
  const restored = await render(bridge); expect(restored.get('h1').text()).toBe('设置');
});
test('hello failure shows full startup error and retry can recover', async () => {
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge);
  let fail = true;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    if (name === 'app.hello' && fail) return { ok: false, error: { code: 'INTERNAL_ERROR', message: 'test', retryable: true } } as never;
    return original(name, payload);
  });
  const wrapper = await render(bridge);
  expect(wrapper.get('h1').text()).toBe('启动失败');
  expect(wrapper.get('[role="alert"]').text()).toContain('日志');
  expect(wrapper.text()).toContain('备份恢复账本');
  expect(wrapper.text()).not.toContain('重置'); expect(wrapper.find('nav').exists()).toBe(false);
  fail = false; await wrapper.get('button').trigger('click'); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('Codex 统计');
});
test('protocol mismatch stops preferences read and every write, including direct calls', async () => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call').mockResolvedValue({ ok: true, data: { protocol: 2 } } as never);
  const wrapper = await render(bridge);
  expect(wrapper.get('h1').text()).toBe('界面与应用版本不兼容');
  expect(call.mock.calls.map(([name]) => name)).toEqual(['app.hello']);
  const guarded = createStartupBridge(bridge);
  await expect(guarded.bridge.call('preferences.update', { operationId: crypto.randomUUID(), expectedRevision: 0, patch: { theme: 'dark' } })).rejects.toMatchObject({ code: 'PROTOCOL_MISMATCH' });
  expect(call).toHaveBeenCalledTimes(1);
});
test('contract mismatch is an incompatibility page; failed preference load stays in startup', async () => {
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge);
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    if (name === 'preferences.get') return { ok: false, error: { code: 'CONTRACT_MISMATCH', message: 'old', retryable: false } } as never;
    return original(name, payload);
  });
  const wrapper = await render(bridge); expect(wrapper.get('h1').text()).toBe('界面与应用版本不兼容');
});
test('explicit hash supports detail/model routes; invalid hashes recover to Codex', async () => {
  history.replaceState(null, '', '/#/models?theme=dark');
  const wrapper = await render(); expect(wrapper.get('h1').text()).toBe('模型池');
  location.hash = '#/wallet/example'; window.dispatchEvent(new HashChangeEvent('hashchange')); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('供应商钱包');
  expect(wrapper.text()).not.toContain('example');
  location.hash = '#/unknown'; window.dispatchEvent(new HashChangeEvent('hashchange')); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('Codex 统计');
});
test('automatic collapse never writes preferences; manual collapse persists and survives resize', async () => {
  const bridge = createMockBridge(); const call = vi.spyOn(bridge, 'call');
  vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(1280);
  const wrapper = await render(bridge); expect(wrapper.get('.shell').classes()).not.toContain('collapsed');
  await wrapper.get('button[aria-label="折叠导航"]').trigger('click'); await flushPromises();
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.collapsed).toBe(true);
  await wrapper.get('button[aria-label="展开导航"]').trigger('click'); await flushPromises();
  const before = call.mock.calls.filter(([name]) => name === 'preferences.update').length;
  vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(1024); window.dispatchEvent(new Event('resize')); await flushPromises();
  expect(wrapper.get('.shell').classes()).toContain('collapsed');
  expect(call.mock.calls.filter(([name]) => name === 'preferences.update')).toHaveLength(before);
  vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(1280); window.dispatchEvent(new Event('resize')); await flushPromises();
  expect(wrapper.get('.shell').classes()).not.toContain('collapsed');
});
test('theme saves immediately; a failed save rolls back and navigation stays usable', async () => {
  const bridge = createMockBridge(); const original = bridge.call.bind(bridge);
  let fail = false;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    if (name === 'preferences.update' && fail) return { ok: false, error: { code: 'INTERNAL_ERROR', message: 'test', retryable: true } } as never;
    return original(name, payload);
  });
  const wrapper = await render(bridge);
  await wrapper.get('nav a[aria-label="设置"]').trigger('click'); await flushPromises();
  await wrapper.get('select[aria-label="显示主题"]').setValue('dark'); await flushPromises();
  expect(document.documentElement.dataset.theme).toBe('dark');
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.theme).toBe('dark');
  fail = true; await wrapper.get('select[aria-label="显示主题"]').setValue('light'); await flushPromises();
  expect(document.documentElement.dataset.theme).toBe('dark'); expect(wrapper.get('[role="alert"]').text()).toContain('未能保存主题');
  await wrapper.get('nav a[aria-label="数据统计与分析"]').trigger('click', { button: 0 }); await flushPromises();
  expect(wrapper.get('h1').text()).toBe('数据统计与分析');
});
test('new user reminder repeatedly opens the guide and dismissal persists', async () => {
  const bridge = createUnpromptedBridge({ migration: { detected: false } }); const wrapper = await render(bridge);
  expect(document.querySelector('dialog[open]')).toBeNull();
  expect(unwrap(await bridge.call('app.hello', { clientProtocol: 1 })).capabilities).not.toContain('history.scan');
  const open = () => wrapper.findAll('button').find(b => b.text() === '打开使用指南')!;
  await open().trigger('click'); await flushPromises();
  expect(document.querySelector('dialog[open]')?.textContent).toContain('欢迎使用');
  const titles = Array.from(document.querySelectorAll('dialog h3')).map(n => n.textContent);
  expect(titles.indexOf('配置倍率版本')).toBeLessThan(titles.indexOf('编辑实际计费价格'));
  (Array.from(document.querySelectorAll('dialog[open] button')).find(b => b.textContent === '稍后设置') as HTMLButtonElement).click(); await flushPromises();
  await open().trigger('click'); await flushPromises();
  expect(document.querySelector('dialog[open]')).not.toBeNull();
  (Array.from(document.querySelectorAll('dialog[open] button')).find(b => b.textContent === '稍后设置') as HTMLButtonElement).click(); await flushPromises();
  await wrapper.findAll('button').find(b => b.text() === '不再显示')!.trigger('click'); await flushPromises();
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.guideDismissed).toBe(true);
  wrapper.unmount(); const restored = await render(bridge); expect(restored.text()).not.toContain('打开使用指南');
  await restored.get('nav a[aria-label="设置"]').trigger('click'); await flushPromises();
  await restored.findAll('button').find(b => b.text() === '指南与关于')!.trigger('click'); await flushPromises();
  await restored.findAll('button').find(b => b.text() === '查看使用指南')!.trigger('click'); await flushPromises();
  expect(document.querySelector('dialog[open]')?.textContent).toContain('在模型池配置基准价格');
});
test('models route survives a startup without a hash and cancelled navigation preserves it', async () => {
  const bridge = createMockBridge();
  const wrapper = await render(bridge);
  location.hash = '#/models'; window.dispatchEvent(new HashChangeEvent('hashchange')); await flushPromises();
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.lastRoute).toBe('/models');
  wrapper.unmount(); history.replaceState(null, '', '/');
  const restored = await render(bridge); expect(restored.get('h1').text()).toBe('模型池');
  await restored.get('input[aria-label="gpt-6 输入价格"]').setValue('9');
  await restored.get('nav a[aria-label="供应商与钱包"]').trigger('click'); await flushPromises();
  (document.querySelector('dialog[open] .button--secondary') as HTMLButtonElement).click(); await flushPromises();
  expect(unwrap(await bridge.call('preferences.get', {})).preferences.lastRoute).toBe('/models');
});
