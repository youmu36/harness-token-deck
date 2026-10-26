import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { BridgeKey } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import DataDirectory from '../src/features/settings/DataDirectory.vue';

const wrappers: ReturnType<typeof mount>[] = [];
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.restoreAllMocks(); });

it('folder drafts and cancellation do not move data; confirming a preview sends its frozen parameters', async () => {
  const bridge = createMockBridge(); const spy = vi.spyOn(bridge, 'call');
  const wrapper = mount(DataDirectory, { attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); wrappers.push(wrapper);
  await flushPromises(); await wrapper.get('input').setValue('D:\\中文 数据');
  expect(spy.mock.calls.some(([name]) => name === 'dataDirectory.move')).toBe(false);
  await wrapper.findAll('button').find(b => b.text() === '预检并迁移')!.trigger('click'); await flushPromises();
  expect(wrapper.get('dialog[open]').text()).toContain('旧副本会保留');
  await wrapper.get('dialog[open] .button--secondary').trigger('click'); await flushPromises();
  expect(spy.mock.calls.some(([name]) => name === 'dataDirectory.move')).toBe(false);
  await wrapper.findAll('button').find(b => b.text() === '预检并迁移')!.trigger('click'); await flushPromises();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises();
  expect(wrapper.text()).toContain('数据目录已切换');
  expect(spy.mock.calls.filter(([name]) => name === 'dataDirectory.move')[0]![1]).toMatchObject({ target: 'D:\\中文 数据' });
});

it('write timeout keeps the operation and preview available for the same retry', async () => {
  const bridge = createMockBridge(), original = bridge.call.bind(bridge); let fail = true;
  const spy = vi.spyOn(bridge, 'call').mockImplementation(async (name, payload) => name === 'dataDirectory.move' && fail ? { ok: false, error: { code: 'WRITE_TIMEOUT', message: '仍在处理，请重试', retryable: true } } as never : original(name, payload));
  const wrapper = mount(DataDirectory, { attachTo: document.body, global: { provide: { [BridgeKey as symbol]: bridge }, stubs: { teleport: true } } }); wrappers.push(wrapper);
  await flushPromises(); await wrapper.get('input').setValue('D:\\target');
  await wrapper.findAll('button').find(b => b.text() === '预检并迁移')!.trigger('click'); await flushPromises();
  await wrapper.get('dialog[open] .button--danger').trigger('click'); await flushPromises();
  expect(wrapper.findComponent({name:'ConfirmDialog'}).props('open')).toBe(true); expect(wrapper.get('dialog').text()).toContain('写入等待超时'); fail = false;
  await wrapper.get('dialog .button--danger').trigger('click'); await flushPromises();
  const calls = spy.mock.calls.filter(([name]) => name === 'dataDirectory.move'); expect(calls[0]![1]).toEqual(calls[1]![1]);
});
