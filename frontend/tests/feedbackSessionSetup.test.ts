import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { parseRoute } from '../src/app/router';
import { createMockBridge } from '../src/infra/bridge/mock';
import SetupGuideDialog from '../src/features/settings/SetupGuideDialog.vue';
import PricingPage from '../src/features/pricing/PricingPage.vue';
import WalletPage from '../src/features/wallet/WalletPage.vue';
import WalletDialog from '../src/features/wallet/WalletDialog.vue';
import LocalWalletDialog from '../src/features/wallet/LocalWalletDialog.vue';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach(fn => fn()); vi.restoreAllMocks(); document.body.innerHTML = ''; });
function setup() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }

it('starts with blank past amounts and saves omitted amounts as zero', async () => {
  const bridge = setup(); const call = vi.spyOn(bridge, 'call');
  const wrapper = mount(WalletDialog, { props: { action: 'create', walletRevision: 0, refresh: async () => {}, latest: () => undefined }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount());
  for (const field of ['过往充值金额', '过往消费金额']) expect((wrapper.get(`input[aria-label="${field}"]`).element as HTMLInputElement).value).toBe('');
  await wrapper.get('input[aria-label="名称"]').setValue('测试供应商');
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(call).toHaveBeenCalledWith('wallet.createVendor', expect.objectContaining({ pastTopUp: '0', pastConsumption: '0' }));
});

it('clears a local past amount to zero without rounding an untouched amount', async () => {
  const bridge = setup(); const call = vi.spyOn(bridge, 'call');
  const vendor = unwrap(await bridge.call('wallet.list', {})).vendors[0]!;
  vendor.localWallet = { ...vendor.localWallet!, R:'0', B:'0', C:'0', pastTopUpPico:'1200000000000', pastConsumptionPico:'1234567890123' };
  const wrapper = mount(LocalWalletDialog, { props: { vendor }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount());
  await wrapper.get('input[aria-label="本地过往充值"]').setValue('');
  await wrapper.get('form').trigger('submit'); await flushPromises();
  const payload = call.mock.calls.find(([name]) => name === 'wallet.setLocalTotals')?.[1];
  expect(payload).toMatchObject({ pastTopUpPico:'0' });
  expect(payload).not.toHaveProperty('pastConsumptionPico');
});

it('keeps five initial steps and leaves unpriced help in the reusable guide', async () => {
  const wrapper = mount(SetupGuideDialog, { props: { firstLaunch:true }, global: { stubs: { teleport:true } } });
  cleanup.push(() => wrapper.unmount());
  expect(wrapper.findAll('ol > li')).toHaveLength(5);
  expect(wrapper.text()).not.toContain('日常使用：处理未定价模型');
  expect(wrapper.text()).toContain('个人资料 → 安全 → 访问令牌');
  const paths = ['/models/setup', '/pricing/groups', '/pricing/versions', '/pricing/rates', '/wallet/setup/new'];
  const buttons = wrapper.findAll('ol > li > .button');
  for (let i = 0; i < paths.length; i++) { await buttons[i]!.trigger('click'); expect(wrapper.emitted('navigate')?.[i]).toEqual([paths[i]]); expect(parseRoute(`#${paths[i]}`)?.guideTarget).toBeTruthy(); }
  await wrapper.setProps({ firstLaunch:false });
  expect(wrapper.text()).toContain('日常使用：处理未定价模型');
});

it('opens and focuses the pricing subsection even when it is already mounted', async () => {
  const bridge = setup();
  const wrapper = mount(PricingPage, { attachTo:document.body, global: { provide: { [BridgeKey as symbol]:bridge }, stubs: { teleport:true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  await wrapper.setProps({ guideTarget:'rates' }); await flushPromises();
  expect(wrapper.find('.price-editor').exists()).toBe(true);
  expect(document.activeElement).toBe(wrapper.get('select[aria-label="编辑价格版本"]').element);
  await wrapper.setProps({ guideTarget:'versions' }); await flushPromises();
  expect(wrapper.find('.price-editor').exists()).toBe(false);
  expect(document.activeElement?.textContent).toBe('新增版本');
});

it('focuses the supplier creation action from the guide route', async () => {
  const bridge = setup();
  const wrapper = mount(WalletPage, { props: { guideTarget:'vendor',initialPreferences:unwrap(await bridge.call('preferences.get',{})) }, attachTo:document.body, global: { provide: { [BridgeKey as symbol]:bridge }, stubs: { teleport:true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(document.activeElement?.textContent).toBe('添加供应商');
});
