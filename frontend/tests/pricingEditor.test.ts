import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type Bridge } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import PriceEditor from '../src/features/pricing/PriceEditor.vue';
import ModelRateRow from '../src/features/pricing/ModelRateRow.vue';
import BillingRuleEditor from '../src/features/pricing/BillingRuleEditor.vue';
import PeakHoursDialog from '../src/features/pricing/PeakHoursDialog.vue';
import PricingDialog from '../src/features/pricing/PricingDialog.vue';
import PricingPage from '../src/features/pricing/PricingPage.vue';
import { defaultRule } from '../src/features/pricing/types';
import { toasts, dismiss } from '../src/ui/toastState';
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); toasts.value.forEach(t => dismiss(t.id)); vi.restoreAllMocks(); });
function mock() { const bridge = createMockBridge(); cleanup.push(() => bridge.dispose()); return bridge; }
async function inputs(bridge: Bridge) { const catalog = unwrap(await bridge.call('pricing.catalog', {})); return { version: catalog.groups[0]!.versions[0]!, pool: unwrap(await bridge.call('models.pool', {})), revision: catalog.revision }; }
async function editor(bridge = mock()) { const wrapper = mount(PriceEditor, { props: await inputs(bridge), attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises(); return { wrapper, bridge }; }
const button = (wrapper: ReturnType<typeof mount>, text: string) => wrapper.findAll('button').find(b => b.text() === text)!;
it('bottom add button creates a row whose model selector disables models already in this version', async () => {
  const { wrapper } = await editor();
  const adds = wrapper.findAll('button').filter(b => b.text() === '添加模型行');
  expect(adds).toHaveLength(2); await adds.at(-1)!.trigger('click'); await flushPromises();
  const selectors = wrapper.findAll('select[aria-label="计价模型"]');
  const duplicate = selectors.at(-1)!.findAll('option').find(o => o.attributes('value') === 'gpt-6')!;
  expect(duplicate.attributes('disabled')).toBeDefined(); expect(duplicate.text()).toContain('已添加');
  await wrapper.findAll('button[aria-label="删除模型行"]')[0]!.trigger('click'); await flushPromises();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises();
  expect(wrapper.get('select[aria-label="计价模型"] option[value="gpt-6"]').attributes('disabled')).toBeUndefined();
});
it('only changing multiplier saves hints without appending a price revision', async () => {
  const { wrapper, bridge } = await editor(); await wrapper.get('input[aria-label="倍率"]').setValue('1.75');
  expect(wrapper.text()).toContain('仅更新倍率提示，不产生修订'); expect(wrapper.text()).toContain('未保存修改 1 项');
  await button(wrapper, '保存倍率提示').trigger('click'); await flushPromises();
  const version = unwrap(await bridge.call('pricing.catalog', {})).groups[0]!.versions[0]!;
  expect(version.revisionCount).toBe(1); expect(version.hints).toEqual([{ modelId: 'gpt-6', multiplier: '1.75' }]);
});
it('merges several synchronous input events into one row without losing earlier fields', async () => {
  const { wrapper, bridge } = await editor();
  for (const label of ['输入价格', '缓存命中价格', '输出价格']) {
    const input = wrapper.get(`input[aria-label="${label}"]`).element as HTMLInputElement;
    input.value = '5'; input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  await flushPromises(); await button(wrapper, '保存（追加修订）').trigger('click'); await flushPromises();
  expect(unwrap(await bridge.call('pricing.catalog', {})).groups[0]!.versions[0]!.models[0]).toMatchObject({ input: '5', cache: '5', output: '5' });
});
it('changing a model asks before overwriting, cancellation preserves the row', async () => {
  const bridge = mock(); const input = await inputs(bridge);
  const wrapper = mount(ModelRateRow, { props: { modelValue: { key: 'row', model: input.version.models[0]!, multiplier: '' }, pool: input.pool }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount());
  await wrapper.get('select[aria-label="计价模型"]').setValue('claude-sonnet-4.5'); await flushPromises();
  expect(wrapper.text()).toContain('选择此模型将覆盖已填'); expect(wrapper.emitted('update:modelValue')).toBeUndefined();
  await wrapper.get('dialog[open] .button--secondary').trigger('click'); await flushPromises(); expect(wrapper.emitted('update:modelValue')).toBeUndefined();
  await wrapper.get('select').setValue('claude-sonnet-4.5'); await flushPromises(); await wrapper.get('dialog[open] .button--danger').trigger('click');
  expect(wrapper.emitted('update:modelValue')?.[0]?.[0]).toMatchObject({ model: { modelId: 'claude-sonnet-4.5', input: '3', cache: '0.3', output: '15' } });
});
it('discard and navigation require confirmation; failures retain edited prices and original operationId', async () => {
  const bridge = mock(); const original = bridge.call.bind(bridge); let failure = true;
  const call = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'pricing.saveVersionPrices' && failure ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: 'timeout', retryable: true } } as never : original(name, payload));
  const { wrapper } = await editor(bridge); await wrapper.get('input[aria-label="输入价格"]').setValue('2.5');
  await button(wrapper, '保存（追加修订）').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('写入等待超时'); expect((wrapper.get('input[aria-label="输入价格"]').element as HTMLInputElement).value).toBe('2.5');
  failure = false; await button(wrapper, '保存（追加修订）').trigger('click'); await flushPromises();
  const writes = call.mock.calls.filter(([name]) => name === 'pricing.saveVersionPrices'); expect(writes[0]![1]).toEqual(writes[1]![1]);
  await wrapper.get('input[aria-label="输入价格"]').setValue('4'); await button(wrapper, '放弃修改').trigger('click');
  await flushPromises(); expect(wrapper.get('dialog[open]').text()).toContain('未保存的价格修改将被放弃');
  await wrapper.get('dialog[open] .button--secondary').trigger('click'); expect((wrapper.get('input[aria-label="输入价格"]').element as HTMLInputElement).value).toBe('4');
});
it('disabled groups are read-only and delete reasons are exposed', async () => {
  const bridge = mock(); const wrapper = mount(PricingPage, { global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount()); await flushPromises();
  await button(wrapper, '价格组设置').trigger('click'); await flushPromises();
  expect(button(wrapper, '删除').attributes('disabled')).toBeUndefined(); expect(wrapper.text()).toContain('历史价格和已计费用量保留');
  await wrapper.findAll('.group-list button').at(-1)!.trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('此分组已停用'); expect(wrapper.get('input[aria-label="输入价格"]').attributes('disabled')).toBeDefined();
});
it('activation lists affected Harness and can activate an unused group without assigning one', async () => {
  const bridge = mock(); const catalog = unwrap(await bridge.call('pricing.catalog', {}));
  const wrapper = mount(PricingDialog, { props: { action: 'activate', group: catalog.groups[0]!, version: catalog.groups[0]!.versions[1]!, revision: catalog.revision, refresh: async () => {} }, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount());
  expect(wrapper.text()).toContain('受影响 Harness：Claude Code、Codex、Zcode'); await wrapper.get('form').trigger('submit'); await flushPromises();
  const first = unwrap(await bridge.call('pricing.catalog', {})); expect(first.current.zcode?.versionId).toBe('standard-v2');
  const created = unwrap(await bridge.call('pricing.createGroup', { name: 'Unused', expectedRevision: first.revision, operationId: crypto.randomUUID() }));
  const added = unwrap(await bridge.call('pricing.addVersion', { groupId: created.groupId!, number: 2, expectedRevision: created.revision, operationId: crypto.randomUUID() }));
  unwrap(await bridge.call('pricing.activateVersion', { groupId: created.groupId!, versionId: added.versionId!, expectedRevision: added.revision, operationId: crypto.randomUUID() }));
  expect(unwrap(await bridge.call('pricing.catalog', {})).current).toEqual(first.current);
});
it('peak time cancellation emits no writes and completion updates the rule; empty extra rates explain no fallback', async () => {
  const rule = defaultRule('peak'); const wrapper = mount(PeakHoursDialog, { props: { rule }, global: { stubs: { teleport: true } } }); cleanup.push(() => wrapper.unmount());
  await wrapper.get('input[aria-label="0 时"]').setValue(true); await wrapper.get('dialog .button--secondary').trigger('click'); expect(wrapper.emitted('done')).toBeUndefined();
  await wrapper.get('dialog .button--primary').trigger('click'); expect(wrapper.emitted('done')?.[0]?.[0]).toMatchObject({ hours: [0,9,10,11,12,13,14,15,16,17] }); expect(rule.hours).not.toContain(0);
  const editor = mount(BillingRuleEditor, { props: { modelValue: defaultRule('tier') }, global: { stubs: { teleport: true } } }); cleanup.push(() => editor.unmount()); expect(editor.text()).toContain('其余留空表示未定价，不回退到基础价格');
  for (const label of ['附加输入', '附加缓存命中', '附加输出']) {
    const input = editor.get(`input[aria-label="${label}"]`).element as HTMLInputElement;
    input.value = '2'; input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  expect(editor.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ rates: { input: '2', cache: '2', output: '2' } });
});
it('mock multiplier uses half-up decimal arithmetic and validates overflow', async () => {
  const bridge = mock(); expect(unwrap(await bridge.call('models.fillByMultiplier', { rates: { input: '1.5', cache: '0.000001' }, multiplier: '1.75' }))).toEqual({ input: '2.625', cache: '0.000002', output: null, cacheWrite: null });
  expect(unwrap(await bridge.call('models.fillByMultiplier', { rates: { input: '0.000001' }, multiplier: '0.5' })).input).toBe('0.000001');
  expect(await bridge.call('models.fillByMultiplier', { rates: { input: '999999999' }, multiplier: '2' })).toMatchObject({ ok: false, error: { code: 'RATE_OVERFLOW' } });
});
