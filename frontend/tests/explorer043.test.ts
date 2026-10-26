import { flushPromises as flushVue, mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import OverviewPage from '../src/features/overview/OverviewPage.vue';
import DimensionExplorer from '../src/features/overview/DimensionExplorer.vue';
import { emptyScope } from '../src/features/overview/scope';

async function flushPromises() { await flushVue(); await vi.dynamicImportSettled(); await flushVue(); }
const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).reverse().forEach(fn => fn()); vi.restoreAllMocks(); });

it('applies combined filters once, shows inline model structure, and leaves hidden analysis idle', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const call = vi.spyOn(bridge, 'call');
  const initialPreferences = unwrap(await bridge.call('preferences.get', {}));
  const wrapper = mount(OverviewPage, { props: { initialPreferences }, global: { provide: { [BridgeKey as symbol]: bridge } } });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  const initialCount=call.mock.calls.filter(([name])=>name==='stats.explore').length;
  const group=wrapper.findAll('.scope-filters label').find(label=>label.text().startsWith('示例价格组 A · '))!;
  await group.get('input').setValue(true);
  expect(call.mock.calls.filter(([name])=>name==='stats.explore')).toHaveLength(initialCount);
  await wrapper.findAll('button').find(button=>button.text()==='应用筛选')!.trigger('click');await flushPromises();
  expect(call.mock.calls.filter(([name])=>name==='stats.explore').at(-1)?.[1]).toMatchObject({groupIds:['example-ga']});
  await wrapper.findAll('[aria-label="统计分析子页"] button').find(b=>b.text()==='数据分析')!.trigger('click');await flushPromises();
  await wrapper.findAll('[aria-label="分析主题"] button').find(b=>b.text()==='性价比')!.trigger('click');await flushPromises();
  expect(wrapper.text()).toContain('M Token/USD');expect(wrapper.text()).not.toContain('模型详情');
  expect(wrapper.get('.model-ranking th').text()).toBe('模型');
  const before = call.mock.calls.filter(([name]) => name === 'stats.explore').length;
  bridge.receiveEvent('app.visibility', { visible: false });
  bridge.receiveEvent('usage.changed', { harnesses: ['codex'], revision: 2 }); await flushPromises();
  expect(call.mock.calls.filter(([name]) => name === 'stats.explore')).toHaveLength(before);
});

it('acknowledges a missing group without removing the record or changing its monetary values', async () => {
  const bridge = createMockBridge(); cleanup.push(() => bridge.dispose());
  const original = bridge.call.bind(bridge);
  let acknowledged = false;
  vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => {
    const result = await original(name, payload);
    if (name === 'stats.acknowledgeQuality') acknowledged = true;
    if (name === 'stats.explore' && result.ok) Object.assign(result.data, { quality: [{ id: 'a'.repeat(64), fingerprint: 'b'.repeat(64), date:'2026-10-21', harness:'codex', vendorId:'example-a', groupId:'', versionId:'', modelKey:'gpt-6', displayName:'GPT-6', reasons:['缓存覆盖不完整'], recordCount:'1', acknowledged, costDifferencePico:null }], qualityTotal:1, qualityUnacknowledged:acknowledged ? 0 : 1 });
    return result;
  });
  const options = unwrap(await bridge.call('stats.exploreOptions', {}));
  const wrapper = mount(DimensionExplorer, { props: { scope: emptyScope(), range:{start:'2026-10-20T00:00:00.000Z',end:'2026-10-22T00:00:00.000Z',offsetMinutes:480,crossesDst:false}, harness:'all', mode:'UTC', options, kind:'quality' }, global:{provide:{[BridgeKey as symbol]:bridge}} });
  cleanup.push(() => wrapper.unmount()); await flushPromises();
  expect(wrapper.get('[aria-label="缺失记录列表"]').text()).toContain('2026-10-21');
  await wrapper.findAll('button').find(button => button.text() === '已知缺失，关闭提醒')!.trigger('click'); await flushPromises();
  expect(wrapper.get('[aria-label="缺失记录列表"]').text()).toContain('已确认');
  expect(wrapper.get('[aria-label="缺失记录列表"]').text()).toContain('缓存覆盖不完整');
  expect(wrapper.text()).toContain('0 组未确认');
});
