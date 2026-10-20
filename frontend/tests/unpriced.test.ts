import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey, type CommandData } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import UnpricedTab from '../src/features/pricing/UnpricedTab.vue';
import HelpHint from '../src/ui/HelpHint.vue';
import { amountExplanation, noteExplanation } from '../src/features/wallet/entryNotes';

const cleanups: (() => void)[] = [];
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); vi.restoreAllMocks(); document.body.innerHTML = ''; });

it('edits an orphan model using pool multipliers and preserves its draft after a failed save', async () => {
  const bridge = createMockBridge(); cleanups.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  const response: CommandData<'pricing.unpriced'> = { ignoredCount: 0, usageRevision: 4, recordCount: 2, readyCount: 0, pricingRevision: 1,
    limited:false, start: '2026-10-01T00:00:00.000Z', end: '2026-10-17T00:00:00.000Z', archived: [], items: [
      { id: 'c'.repeat(64), harness: 'codex', modelId: 'gpt-6', groupId: null, versionId: null, groupName: '未选择价格组', versionName: '未选择版本', recordCount: 2, readyCount: 0, reasons: ['请确认历史归属'], firstAt: '2026-10-01T00:00:00.000Z', lastAt: '2026-10-01T00:00:00.000Z', editable: true, ignored: false, price: null, multiplier: '' },
    ] };
  let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (command, payload) => {
    if (command === 'pricing.unpriced') return { ok: true, data: response } as never;
    if (command === 'pricing.resolveUnpriced') return (fail ? { ok: false, error: { code: 'PRICING_REVISION_CONFLICT', message: '价格目录已变化', retryable: false } } : { ok: true, data: { changed: 2, revision: 5 } }) as never;
    return original(command, payload);
  });
  const wrapper = mount(UnpricedTab, { attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanups.push(() => wrapper.unmount()); await flushPromises();
  await wrapper.findAll('tbody button').find(b => b.text() === '编辑价格')!.trigger('click'); await flushPromises();
  const dialog = () => document.querySelector<HTMLDialogElement>('dialog[open]')!;
  expect((dialog().querySelector('[aria-label=价格组]') as HTMLSelectElement).value).toBe('htu-history');
  expect(dialog().textContent).toContain('模型池价格'); expect(dialog().textContent).toContain('不写入资金流水');
  const multiplier = dialog().querySelector<HTMLInputElement>('[aria-label=倍率]')!; multiplier.value = '2'; multiplier.dispatchEvent(new Event('input', { bubbles: true })); await flushPromises();
  [...dialog().querySelectorAll('button')].find(b => b.textContent?.includes('按倍率填充'))!.click(); await flushPromises();
  expect((dialog().querySelector('[aria-label=输入价格]') as HTMLInputElement).value).toBe('3');
  dialog().querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await flushPromises();
  expect(dialog().textContent).toContain('价格目录已更新');
  expect((dialog().querySelector('[aria-label=倍率]') as HTMLInputElement).value).toBe('2');
  fail = false;
  dialog().querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await flushPromises();
  const request = spy.mock.calls.filter(([name]) => name === 'pricing.resolveUnpriced').at(-1)![1];
  expect(request).toMatchObject({ action: 'price', groupId: 'htu-history', versionId: 'htu-history-v1', expectedUsageRevision: 4, multiplier: '2', model: { modelId: 'gpt-6', input: '3', output: '12' } });
  expect(document.querySelector('dialog[open]')).toBeNull();
});

it('ignores a chosen request count and restores ignored rows from the list', async () => {
  const bridge = createMockBridge(); cleanups.push(() => bridge.dispose()); const original = bridge.call.bind(bridge);
  let ignored = false;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (command, payload) => {
    if (command === 'pricing.unpriced') return { ok: true, data: { ignoredCount: ignored ? 1 : 0, usageRevision: 4, recordCount: ignored ? 0 : 2, readyCount: 0, pricingRevision: 1,
      limited:false, start: '2026-10-01T00:00:00.000Z', end: '2026-10-17T00:00:00.000Z', archived: [], items: [
        { id: 'd'.repeat(64), harness: 'codex', modelId: 'gpt-6', groupId: null, versionId: null, groupName: '未选择价格组', versionName: '未选择版本', recordCount: ignored ? 1 : 2, readyCount: 0, reasons: [], firstAt: '2026-10-01T00:00:00.000Z', lastAt: '2026-10-01T00:00:00.000Z', editable: true, ignored, price: null, multiplier: '' },
      ] } } as never;
    if (command === 'pricing.resolveUnpriced') { ignored = (payload as { action: string }).action === 'ignore'; return { ok: true, data: { changed: 1, revision: 5 } } as never; }
    return original(command, payload);
  });
  const wrapper = mount(UnpricedTab, { attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanups.push(() => wrapper.unmount()); await flushPromises();
  await wrapper.findAll('tbody button').find(b => b.text() === '忽略…')!.trigger('click'); await flushPromises();
  const input = document.querySelector<HTMLInputElement>('dialog[open] [aria-label=忽略条数]')!; input.value = '1'; input.dispatchEvent(new Event('input', { bubbles: true }));
  document.querySelector('dialog[open] form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await flushPromises();
  expect(spy.mock.calls.find(([name]) => name === 'pricing.resolveUnpriced')![1]).toMatchObject({ action: 'ignore', recordLimit: 1 });
  expect(wrapper.findAll('tbody tr')).toHaveLength(0);
  await wrapper.get('.check input').setValue(true); await flushPromises();
  await wrapper.findAll('tbody button').find(b => b.text() === '取消忽略')!.trigger('click'); await flushPromises();
  expect(spy.mock.calls.filter(([name]) => name === 'pricing.resolveUnpriced').at(-1)![1]).toMatchObject({ action: 'restore' });
  expect(wrapper.text()).not.toContain('取消忽略');
});

it('lists each original version and requests recovery only for the displayed pricing revision', async () => {
  const bridge = createMockBridge(); cleanups.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  let done = false;
  const response: CommandData<'pricing.unpriced'> = { ignoredCount: 0, usageRevision: 2, recordCount: 3, readyCount: 2, pricingRevision: 12,
    limited:false, start: '2026-09-16T00:00:00.000Z', end: '2026-10-16T00:00:00.000Z', archived: [], items: [
      { id: 'a'.repeat(64), ignored: false, price: null, multiplier: '', harness: 'codex', modelId: 'example-model', groupId: 'g', versionId: 'v1', groupName: '原分组', versionName: '旧价格', recordCount: 1, readyCount: 0, reasons: ['请补充输入价格'], firstAt: '2026-10-01T00:00:00.000Z', lastAt: '2026-10-01T00:00:00.000Z', editable: true },
      { id: 'b'.repeat(64), ignored: false, price: null, multiplier: '', harness: 'codex', modelId: 'example-model', groupId: 'g', versionId: 'v2', groupName: '原分组', versionName: 'v2', recordCount: 2, readyCount: 2, reasons: [], firstAt: '2026-10-02T00:00:00.000Z', lastAt: '2026-10-02T00:00:00.000Z', editable: true },
    ] };
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (command, payload) => {
    if (command === 'pricing.unpriced') return { ok: true, data: done ? { ...response, items: [response.items[0]], readyCount: 0, recordCount: 1 } : response } as never;
    if (command === 'pricing.recalculateUnpriced') { done = true; return { ok: true, data: { recalculated: 2, remaining: 1, costDeltaPico: '1230000000000', revision: 10 } } as never; }
    return original(command, payload);
  });
  const wrapper = mount(UnpricedTab, { global: { provide: { [BridgeKey as symbol]: bridge } } }); cleanups.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.findAll('tbody tr')).toHaveLength(2);
  expect(wrapper.text()).toContain('旧价格'); expect(wrapper.text()).toContain('价格已补齐');
  await wrapper.findAll('tbody button').filter(button => button.text() === '前往补价')[1]!.trigger('click');
  expect(wrapper.emitted('edit')![0]).toEqual(['g', 'v2']);
  await wrapper.get('.button--primary').trigger('click'); await flushPromises();
  const request = spy.mock.calls.find(([name]) => name === 'pricing.recalculateUnpriced')![1];
  expect(request).toMatchObject({ expectedPricingRevision: 12, operationId: expect.any(String) });
  expect(wrapper.text()).toContain('已重算 2 条'); expect(wrapper.findAll('tbody tr')).toHaveLength(1);
  expect(wrapper.get('.button--primary').attributes('disabled')).toBeDefined();
});

it('the amount and calibration hints explain both signs and support hover and keyboard focus', async () => {
  const entry = { kind: 'consumption_daily' } as CommandData<'wallet.entries'>['entries'][number];
  const amount = amountExplanation(entry); const note = noteExplanation(entry);
  expect(amount).toContain('负数'); expect(amount).toContain('正数增加余额');
  expect(note).toContain('保留原消费'); expect(note).toContain('同一笔修正只计算一次');
  const wrapper = mount(HelpHint, { props: { label: '备注含义', text: note } }); cleanups.push(() => wrapper.unmount());
  await wrapper.trigger('mouseenter'); await flushPromises();
  expect(document.querySelector('[role=tooltip]')?.textContent).toBe(note);
  expect(wrapper.get('button').attributes('aria-describedby')).toBe(document.querySelector('[role=tooltip]')?.id);
  await wrapper.trigger('mouseleave'); expect(document.querySelector('[role=tooltip]')).toBeNull();
  await wrapper.trigger('focusin'); await flushPromises(); expect(document.querySelector('[role=tooltip]')).not.toBeNull();
  await wrapper.trigger('keydown', { key: 'Escape' }); expect(document.querySelector('[role=tooltip]')).toBeNull();
});
