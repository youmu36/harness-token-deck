import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import RemoteAccountDialog from '../src/features/wallet/RemoteAccountDialog.vue';
import RemoteBalanceRow from '../src/features/wallet/RemoteBalanceRow.vue';
import WalletDialog from '../src/features/wallet/WalletDialog.vue';
import WalletPage from '../src/features/wallet/WalletPage.vue';
import VendorEntries from '../src/features/wallet/VendorEntries.vue';
import { remoteAmount } from '../src/features/wallet/remote';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
async function supplier(bridge: Bridge) { return unwrap(await bridge.call('wallet.list', {})).vendors[0]!; }
async function saved(bridge: Bridge, mode: 'api_key' | 'access_token' = 'api_key') { const v = await supplier(bridge); await bridge.call('remote.save', { vendorId: v.id, baseUrl: 'https://example.test', authMode: mode, siteType: mode === 'access_token' ? 'newapi' : 'sub2api', userId: mode === 'access_token' ? '123' : null, credential: 'synthetic-secret-abcd', enabled: false, operationId: crypto.randomUUID() }); return v; }
async function dialog(bridge: Bridge) { const v = await supplier(bridge); const wrapper = mount(RemoteAccountDialog, { props: { vendorId: v.id, vendorName: v.name }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises(); return wrapper; }
it('accepts one minute and explains the retry threshold before saving', async () => {
  const bridge = mock(); const wrapper = await dialog(bridge);
  await wrapper.get('input[aria-label="站点地址"]').setValue('https://example.test');
  await wrapper.get('select[aria-label="接口类型"]').setValue('sub2api');
  await wrapper.get('input[aria-label="令牌"]').setValue('synthetic-key-abcd');
  const interval = wrapper.get('input[aria-label="刷新间隔"]');
  expect(interval.attributes('min')).toBe('1');
  await interval.setValue(1);
  expect(wrapper.text()).toContain('不自动重试');
  await interval.setValue(5);
  expect(wrapper.text()).toContain('最多两次');
  await interval.setValue(1);
  const call = vi.spyOn(bridge, 'call');
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(call.mock.calls.find(([name]) => name === 'remote.save')?.[1]).toMatchObject({refreshMinutes:1});
  expect(wrapper.emitted('saved')).toHaveLength(1);
});
it('remote balance increases create one exact top-up and show an automatic recharge note', async () => {
  const bridge = mock();
  const created = unwrap(await bridge.call('wallet.createVendor', { name: 'Auto recharge', initialBalance: '0', operationId: crypto.randomUUID() }));
  const vendorId = created.vendorId;
  await bridge.call('remote.save', { vendorId, baseUrl: 'https://example.test', authMode: 'api_key', siteType: 'sub2api', credential: 'synthetic-secret-abcd', enabled: false, operationId: crypto.randomUUID() });
  const refresh = { vendorId, operationId: crypto.randomUUID() };
  const first = await bridge.call('remote.refresh', refresh);
  expect(await bridge.call('remote.refresh', refresh)).toEqual(first);
  await bridge.call('remote.refresh', { vendorId, operationId: crypto.randomUUID() });
  const v = unwrap(await bridge.call('wallet.list', {})).vendors.find(item => item.id === vendorId)!;
  expect(v).toMatchObject({ R: '1975310000001', A: '0', B: '1975310000001' });
  const rows = unwrap(await bridge.call('wallet.entries', { vendorId, kind: 'top_up' })).entries;
  expect(rows).toHaveLength(1); expect(rows[0]).toMatchObject({ amountPico: '1975310000001', note: '远程余额增加，自动补记充值' });
  const wrapper = mount(VendorEntries, { props: { vendor: v, mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.text()).toContain('远程余额增加，自动补记充值'); expect(wrapper.text()).not.toContain('校准调整');
});
it('incomplete saves show required fields and never send a write; tests scroll to their result', async () => {
  const bridge = mock(); const wrapper = await dialog(bridge); const call = vi.spyOn(bridge, 'call');
  expect(wrapper.get('button[form="remote-account"]').attributes('disabled')).toBeUndefined();
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(wrapper.get('[role="alert"]').text()).toContain('有必填项未完成');
  expect(wrapper.get('input[aria-label="站点地址"]').attributes('aria-invalid')).toBe('true');
  expect(call.mock.calls.some(([name]) => name === 'remote.save')).toBe(false);
  expect(wrapper.find('option[value="auto"]').exists()).toBe(false);
  await wrapper.get('input[aria-label="站点地址"]').setValue('https://example.test');
  await wrapper.get('select[aria-label="接口类型"]').setValue('newapi');
  await wrapper.get('input[aria-label="用户 ID"]').setValue('42');
  await wrapper.get('input[aria-label="令牌"]').setValue('synthetic-token-abcd');
  const scroll = vi.spyOn(HTMLElement.prototype, 'scrollTo');
  await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises();
  expect(wrapper.get('.test-result').text()).toContain('连接成功'); expect(scroll).toHaveBeenCalled();
  expect(wrapper.text()).toContain('保存后按余额偏差校准');
  expect(wrapper.text()).toContain('大于 1 USD 的条件才自动补记充值');
});
it('saved credentials display only four trailing characters and a blank password retains the vault value', async () => {
  const bridge = mock(); const v = await saved(bridge); const wrapper = await dialog(bridge);
  expect(wrapper.text()).toContain('已保存 ••••abcd'); expect(wrapper.html()).not.toContain('synthetic-secret-abcd');
  expect((wrapper.get('input[aria-label="令牌"]').element as HTMLInputElement).value).toBe('');
  const call = vi.spyOn(bridge, 'call'); await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(call.mock.calls.find(([name]) => name === 'remote.save')?.[1]).toMatchObject({ credential: null });
  expect(unwrap(await bridge.call('remote.get', { vendorId: v.id })).account?.maskedCredential).toBe('••••abcd');
});
it.each(['sub2api', 'newapi'] as const)('selecting %s sets its authentication fields and persists the selected type', async siteType => {
  const bridge = mock(); const wrapper = await dialog(bridge);
  expect((wrapper.get('select[aria-label="接口类型"]').element as HTMLSelectElement).value).toBe('');
  await wrapper.get('input[aria-label="站点地址"]').setValue('https://example.test');
  await wrapper.get('input[aria-label="令牌"]').setValue('synthetic-key-abcd');
  await wrapper.get('select[aria-label="接口类型"]').setValue(siteType); await flushPromises();
  expect(wrapper.find('select[aria-label="查询方式"]').exists()).toBe(false);
  expect(wrapper.find('input[aria-label="用户 ID"]').exists()).toBe(siteType === 'newapi');
  if (siteType === 'newapi') await wrapper.get('input[aria-label="用户 ID"]').setValue('42');
  const call = vi.spyOn(bridge, 'call'); await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(call.mock.calls.find(([name]) => name === 'remote.save')?.[1]).toMatchObject({ siteType, authMode: siteType === 'newapi' ? 'access_token' : 'api_key', userId: siteType === 'newapi' ? '42' : null });
  const reopened = await dialog(bridge);
  expect((reopened.get('select[aria-label="接口类型"]').element as HTMLSelectElement).value).toBe(siteType);
});
it('changing the selected type clears a prior test result and always uses a fixed authentication mode', async () => {
  const bridge = mock(); await saved(bridge, 'access_token'); const wrapper = await dialog(bridge);
  await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises();
  expect(wrapper.find('.test-result').exists()).toBe(true);
  await wrapper.get('select[aria-label="接口类型"]').setValue('sub2api'); await flushPromises();
  expect(wrapper.find('.test-result').exists()).toBe(false);
  await wrapper.get('input[aria-label="令牌"]').setValue('synthetic-new-secret-abcd');
  await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises();
  expect(wrapper.get('.test-result').text()).toContain('账户剩余额度');
  expect(wrapper.get('.test-result').text()).not.toContain('此结果不能用于一键校准');
  await wrapper.get('select[aria-label="接口类型"]').setValue('newapi'); await flushPromises();
  expect(wrapper.find('select[aria-label="查询方式"]').exists()).toBe(false);
});
it.each([['api_key', '账户剩余额度'], ['access_token', '账户剩余额度']] as const)('connection tests show the Chinese meaning of %s', async (mode, label) => {
  const bridge = mock(); await saved(bridge, mode); const wrapper = await dialog(bridge);
  await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain(`连接成功 · ${label}`);
});
it.each([
  ['USD', false, '$1.9753'],
  ['USD', true, '1.975310 （币种未确认）'],
  [null, true, '1.975310 （币种未确认）'],
  [null, false, '1.975310 （币种未确认）'],
  ['CNY', false, '1.975310 CNY'],
] as const)('cards and connection results render currency %s with uncertainty %s accurately', async (currency, uncertain, expected) => {
  const bridge = mock(); const v = await saved(bridge); await bridge.call('remote.refresh', { vendorId: v.id, operationId: crypto.randomUUID() });
  const original = bridge.call.bind(bridge); const source = unwrap(await original('remote.get', { vendorId: v.id }));
  source.observation!.currency = currency; source.observation!.currencyUncertain = uncertain; source.observation!.canCalibrate = currency === 'USD' && !uncertain;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'remote.get' ? { ok: true, data: source } as never : name === 'remote.test' ? { ok: true, data: { observation: source.observation, error: null } } as never : original(name, payload));
  const row = mount(RemoteBalanceRow, { props: { vendorId: v.id, mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => row.unmount()); await flushPromises();
  expect(row.get('.money').text()).toBe(expected);
  const wrapper = await dialog(bridge); await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises();
  expect(wrapper.get('.test-result .money').text()).toBe(expected);
  if (uncertain || currency !== 'USD') expect(row.text()).not.toContain('按此余额校准');
});
it.each(['billing', 'unlimited', 'quota_units', 'stale', 'offline'] as const)('does not offer calibration for %s', async scenario => {
  const bridge = mock(); const v = await saved(bridge);
  await bridge.call('remote.refresh', { vendorId: v.id, operationId: crypto.randomUUID() });
  const original = bridge.call.bind(bridge); const source = unwrap(await original('remote.get', { vendorId: v.id }));
  if (scenario === 'billing') { source.observation!.kind = 'subscription_remaining'; source.observation!.canCalibrate = false; } else { source.status = scenario === 'offline' ? 'offline' : scenario === 'stale' ? 'stale' : 'ok'; source.observation!.kind = ['unlimited', 'quota_units'].includes(scenario) ? scenario as 'unlimited' | 'quota_units' : 'token_remaining'; source.observation!.canCalibrate = false; source.observation!.stale = scenario === 'stale'; if (scenario === 'offline') source.error = { code: 'offline', message: '无法连接', statusCode: null, retryAfterSeconds: null }; }
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'remote.get' ? { ok: true, data: source } as never : original(name, payload));
  const wrapper = mount(RemoteBalanceRow, { props: { vendorId: v.id, mode: 'UTC' }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.text()).not.toContain('按此余额校准');
});
it('remote calibration pre-fills the display amount and sends the observation timestamp, preserving full pico', async () => {
  const bridge = mock(); const v = await saved(bridge); const snapshot = unwrap(await bridge.call('remote.refresh', { vendorId: v.id, operationId: crypto.randomUUID() })); const current = await supplier(bridge);
  const wrapper = mount(WalletDialog, { props: { action: 'calibrate', vendor: current, walletRevision: current.walletRevision, remoteObservation: snapshot.observation!, refresh: async () => {}, latest: () => current }, attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect((wrapper.get('input').element as HTMLInputElement).value).toBe('1.975310'); expect(wrapper.text()).toContain('example.test');
  await wrapper.get('form').trigger('submit'); await flushPromises(); expect((await supplier(bridge)).B).toBe('1975310000001');
  expect(remoteAmount('-1975310999999')).toBe('-1.975310'); expect(remoteAmount('-1')).toBe('0.000000');
});
it('saving failures preserve drafts and reuse the operation id, and testing cannot save configuration', async () => {
  const bridge = mock(); const original = bridge.call.bind(bridge); let fail = true;
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'remote.save' && fail ? { ok: false, error: { code: 'CREDENTIAL_STORE_UNAVAILABLE', message: 'vault', retryable: false } } as never : original(name, payload));
  const wrapper = await dialog(bridge); await wrapper.get('input[aria-label="站点地址"]').setValue('https://example.test'); await wrapper.get('select[aria-label="接口类型"]').setValue('sub2api'); await wrapper.get('input[aria-label="令牌"]').setValue('test-key-abcd');
  await wrapper.findAll('button').find(b => b.text() === '测试连接')!.trigger('click'); await flushPromises(); expect(call.mock.calls.some(([name]) => name === 'remote.save')).toBe(false);
  await wrapper.get('form').trigger('submit'); await flushPromises(); expect(wrapper.text()).toContain('Windows 凭据存储不可用'); expect((wrapper.get('input[aria-label="令牌"]').element as HTMLInputElement).value).toBe('test-key-abcd');
  fail = false; await wrapper.get('form').trigger('submit'); await flushPromises(); const writes = call.mock.calls.filter(([name]) => name === 'remote.save'); expect(writes[0]![1]).toEqual(writes[1]![1]);
});
it('wallet card routes configuration and a successful remote observation into calibration', async () => {
  const bridge = mock(); const v = await saved(bridge); await bridge.call('remote.refresh', { vendorId: v.id, operationId: crypto.randomUUID() });
  const wrapper = mount(WalletPage, {props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))}, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  await wrapper.findAll('[aria-label="钱包分类"] button').find(b=>b.text()==='远程钱包')!.trigger('click');await flushPromises();
  await wrapper.findAll('[aria-label="供应商二级导航"] button').find(b => b.text() === '远程账户')!.trigger('click'); await flushPromises();
  await wrapper.findAll('button').find(b => b.text() === '按此余额校准')!.trigger('click'); await flushPromises(); expect(wrapper.get('input[aria-label="金额"]').element).toHaveProperty('value', '1.975310');
});
