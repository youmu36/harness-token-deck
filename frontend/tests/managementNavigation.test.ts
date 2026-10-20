import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import PricingPage from '../src/features/pricing/PricingPage.vue';
import WalletPage from '../src/features/wallet/WalletPage.vue';
import HarnessPage from '../src/features/harness/HarnessPage.vue';

const cleanup: (() => void)[] = [];
afterEach(() => cleanup.splice(0).forEach(fn => fn()));
async function pricing() {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const wrapper = mount(PricingPage, { global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises(); await wrapper.get('[aria-label="价格组二级导航"] button:nth-child(2)').trigger('click'); await flushPromises(); return { wrapper, bridge };
}
it('secondary pricing navigation retains a cancelled draft and confirms before unmounting it', async () => {
  const { wrapper } = await pricing();
  await wrapper.get('input[aria-label="输入价格"]').setValue('2.75');
  const versions = wrapper.get('[aria-label="价格组二级导航"] button:first-child');
  await versions.trigger('click'); await flushPromises();
  expect(wrapper.get('dialog[open]').text()).toContain('未保存的价格修改');
  await wrapper.get('dialog[open] .button--secondary').trigger('click'); await flushPromises();
  expect((wrapper.get('input[aria-label="输入价格"]').element as HTMLInputElement).value).toBe('2.75');
  await versions.trigger('click'); await flushPromises();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises();
  expect(wrapper.find('.price-editor').exists()).toBe(false); expect(wrapper.find('.version-list').exists()).toBe(true);
});
it('clicking the selected pricing section or group does not discard the draft', async () => {
  const { wrapper } = await pricing(); await wrapper.get('input[aria-label="输入价格"]').setValue('3.75');
  await wrapper.get('[aria-label="价格组二级导航"] button:nth-child(2)').trigger('click');
  await wrapper.get('.group-list button[aria-pressed="true"]').trigger('click'); await flushPromises();
  expect(wrapper.find('dialog[open]').exists()).toBe(false);
  expect((wrapper.get('input[aria-label="输入价格"]').element as HTMLInputElement).value).toBe('3.75');
});
it('a cancelled version change restores both the selection and its edited fields', async () => {
  const { wrapper } = await pricing(); const selector = wrapper.get('select[aria-label="编辑价格版本"]');
  const original = (selector.element as HTMLSelectElement).value;
  const other = wrapper.findAll('select[aria-label="编辑价格版本"] option').find(option => option.attributes('value') !== original)!.attributes('value');
  await wrapper.get('input[aria-label="输入价格"]').setValue('4.75'); await selector.setValue(other); await flushPromises();
  await wrapper.get('dialog[open] .button--secondary').trigger('click'); await flushPromises();
  expect((selector.element as HTMLSelectElement).value).toBe(original);
  expect((wrapper.get('input[aria-label="输入价格"]').element as HTMLInputElement).value).toBe('4.75');
});
it('wallet pages display one selected vendor and load entries only through the secondary menu', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const wrapper = mount(WalletPage, {props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))}, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.findAll('.vendor-card')).toHaveLength(1); expect(wrapper.find('.account-entries').exists()).toBe(false);
  await wrapper.get('[aria-label="供应商二级导航"] button:nth-child(2)').trigger('click'); await flushPromises();
  expect(wrapper.find('.account-entries').exists()).toBe(true); expect(wrapper.find('.vendor-card').exists()).toBe(false);
  await wrapper.findAll('[aria-label="供应商二级导航"] button').find(button=>button.text()==='供应商设置')!.trigger('click'); await flushPromises();
  expect(wrapper.find('.vendor-settings').exists()).toBe(true); expect(wrapper.text()).toContain('关联只影响之后入账的请求');
});
it('archiving keeps records accessible and removes remote actions from the archived menu', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const vendor = unwrap(await bridge.call('wallet.list', {})).vendors[0]!;
  const wrapper = mount(WalletPage, {props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))}, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  unwrap(await bridge.call('wallet.deleteVendor', { vendorId: vendor.id, operationId: crypto.randomUUID() })); await flushPromises();
  await wrapper.get('[aria-label="供应商分类"] button:nth-child(2)').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain(vendor.name); expect(wrapper.text()).toContain('归档记录 · 只读');
  expect(wrapper.get('[aria-label="供应商二级导航"]').text()).not.toContain('远程账户');
  await wrapper.findAll('.vendor-card footer button').find(button => button.text() === '查看流水')!.trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('资金流水');
});
it('a newly created vendor becomes the selected wallet instead of leaving the previous one active', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const wrapper = mount(WalletPage, {props:{initialPreferences:unwrap(await bridge.call('preferences.get',{}))}, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  await wrapper.get('header .button--primary').trigger('click'); await flushPromises();
  await wrapper.get('input[aria-label="名称"]').setValue('新供应商');
  await wrapper.get('form').trigger('submit'); await flushPromises();
  expect(wrapper.get('.vendor-heading h2').text()).toBe('新供应商');
  expect(wrapper.get('.vendor-list button[aria-pressed="true"]').text()).toContain('新供应商');
});
it('Codex statistics remain free of silent title request counts and notes', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const wrapper = mount(HarnessPage, { props: { harness: 'codex', initialPreferences: unwrap(await bridge.call('preferences.get', {})) }, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.text()).not.toContain('标题生成'); expect(wrapper.find('.coverage-note').exists()).toBe(false);
});
