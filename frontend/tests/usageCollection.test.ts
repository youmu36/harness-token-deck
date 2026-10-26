import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { createMockBridge } from '../src/infra/bridge/mock';
import { createValidatedBridge } from '../src/infra/bridge/core';
import { createEventHub } from '../src/shared/events';
import ScanButton from '../src/features/usage-common/ScanButton.vue';
import CollectionStatus from '../src/features/usage-common/CollectionStatus.vue';
import { collectionIndicator, diagnosticText } from '../src/features/usage-common/collectionNotes';
import { collectionStatus } from './usageFixtures';

afterEach(() => { vi.useRealTimers(); });
describe('collection controls', () => {
  it.each([
    ['watching', '监测中'], ['baselining', '正在建立检查点'], ['degraded', '采集需关注'],
    ['unavailable', '来源不可用'], ['pending', '等待连接'],
  ] as const)('shows source status %s', (status, label) => {
    const snapshot = collectionStatus(); snapshot.sources[0]!.status = status;
    expect(collectionIndicator(snapshot, 'codex').label).toBe(label);
  });
  it('prioritizes pause and overview attention states and preserves unknown diagnostic codes', () => {
    const snapshot = collectionStatus(); snapshot.sources[1]!.status = 'degraded';
    expect(collectionIndicator(snapshot, 'all').label).toBe('采集需关注');
    snapshot.paused = true;
    expect(collectionIndicator(snapshot, 'all').label).toBe('采集已暂停');
    expect(collectionIndicator(undefined, 'codex').label).toBe('等待连接');
    expect(diagnosticText('desktop_source_unverified')).toContain('Codex 桌面端');
    expect(diagnosticText('future_diagnostic')).toBe('future_diagnostic');
  });
  it('opens source diagnostics, shows actual counts and times, and closes with Escape', async () => {
    const snapshot = collectionStatus(); snapshot.sources[0]!.diagnostics = ['source_read_failed', 'future_diagnostic'];
    snapshot.sources[0]!.inboxCount = 5;
    const wrapper = mount(CollectionStatus, { props: { harness: 'codex', status: snapshot, mode: 'UTC' } });
    await wrapper.get('button').trigger('click');
    expect(wrapper.get('[role="region"]').text()).toContain('无法读取来源文件，等待重试');
    expect(wrapper.text()).toContain('future_diagnostic');
    expect(wrapper.text()).toContain('已入账 24 条、待计费 5 条');
    expect(wrapper.text()).toContain('运行中的数字可能暂时保持不变，完成后会自动补齐');
    expect(wrapper.text()).toContain('2026-10-06 06:32');
    expect(wrapper.get('a').attributes('href')).toBe('#/settings/background');
    await wrapper.trigger('keydown', { key: 'Escape' });
    expect(wrapper.find('[role="region"]').exists()).toBe(false);
    wrapper.unmount();
  });
  it('counts down from backend deadlines, handles ticks, hidden recovery, and never schedules a scan', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T06:32:00Z'));
    const hub = createEventHub();
    const bridge = createValidatedBridge('preview', async () => { throw new Error('Unexpected scan'); }, hub);
    const call = vi.spyOn(bridge, 'call');
    const wrapper = mount(ScanButton, { props: { showCountdown: true, nextPeriodicAt: '2026-10-06T06:32:17Z' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
    const ring = wrapper.get('.countdown-ring');
    const sweep = () => parseFloat((ring.element as HTMLElement).style.getPropertyValue('--countdown-sweep'));
    expect(wrapper.find('svg circle').exists()).toBe(false);
    expect(sweep()).toBeCloseTo(17 / 30 * 360);
    expect(wrapper.get('.countdown').text()).toBe('17 秒');
    await vi.advanceTimersByTimeAsync(4_000); expect(wrapper.get('.countdown').text()).toBe('13 秒');
    expect(sweep()).toBeCloseTo(13 / 30 * 360);
    hub.emit('collection.tick', { status: { codex: 'watching', zcode: 'watching', dsh: 'watching' }, nextPeriodicAt: '2026-10-06T06:32:17Z', lastCompletedAt: '2026-10-06T06:32:04Z', trigger: 'notification' });
    await flushPromises(); expect(sweep()).toBeCloseTo(13 / 30 * 360);
    await vi.advanceTimersByTimeAsync(100); expect(sweep()).toBeCloseTo(13 / 30 * 360);
    hub.emit('collection.tick', { status: { codex: 'watching', zcode: 'watching', dsh: 'watching' }, nextPeriodicAt: '2026-10-06T06:32:11Z', lastCompletedAt: null, trigger: 'notification' });
    await flushPromises(); expect(wrapper.get('.countdown').text()).toBe('7 秒');
    expect(sweep()).toBeCloseTo(6.9 / 30 * 360);
    hub.emit('app.visibility', { visible: false }); await flushPromises();
    await vi.advanceTimersByTimeAsync(10_000); expect(wrapper.get('.countdown').text()).toBe('7 秒');
    hub.emit('app.visibility', { visible: true }); await flushPromises();
    expect(wrapper.get('.countdown').text()).toBe('0 秒'); expect(call).not.toHaveBeenCalled();
    expect(sweep()).toBe(0);
    await wrapper.setProps({ paused: true }); expect(wrapper.find('.countdown').exists()).toBe(false); expect(wrapper.get('button').attributes('disabled')).toBeDefined();
    await wrapper.setProps({ paused: false });
    hub.emit('collection.tick', { status: { codex: 'watching', zcode: 'watching', dsh: 'watching' }, nextPeriodicAt: null, lastCompletedAt: null, trigger: 'periodic' });
    await flushPromises(); expect(wrapper.find('.countdown').exists()).toBe(false);
    wrapper.unmount(); expect(vi.getTimerCount()).toBe(0); bridge.dispose();
  });
  it('fills the ring only when the backend starts a new 30-second period', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T06:32:00Z'));
    const bridge = createMockBridge();
    const wrapper = mount(ScanButton, { props: { showCountdown: true, nextPeriodicAt: '2026-10-06T06:32:30Z' }, global: { provide: { [BridgeKey as symbol]: bridge } } });
    const sweep = () => parseFloat((wrapper.get('.countdown-ring').element as HTMLElement).style.getPropertyValue('--countdown-sweep'));
    expect(sweep()).toBe(360);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(wrapper.get('.countdown').text()).toBe('15 秒'); expect(sweep()).toBe(180);
    await wrapper.setProps({ nextPeriodicAt: '2026-10-06T06:32:45Z' });
    expect(sweep()).toBe(360);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(wrapper.get('.countdown').text()).toBe('0 秒'); expect(sweep()).toBe(0);
    wrapper.unmount(); bridge.dispose();
  });
  it('blocks repeat clicks while pending, publishes completion once, and uses its returned deadline', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T06:32:00Z'));
    let finish!: (value: unknown) => void;
    const bridge = createValidatedBridge('preview', () => new Promise(resolve => { finish = resolve; }));
    const call = vi.spyOn(bridge, 'call');
    const wrapper = mount(ScanButton, { props: { showCountdown: true }, global: { provide: { [BridgeKey as symbol]: bridge } } });
    await wrapper.get('button').trigger('click'); await wrapper.get('button').trigger('click');
    expect(call).toHaveBeenCalledTimes(1);
    expect(wrapper.get('button').attributes('disabled')).toBeDefined(); expect(wrapper.find('.spinning').exists()).toBe(true);
    expect(wrapper.emitted('completed')).toBeUndefined();
    const result = { completedAt: '2026-10-06T06:32:00.000Z', nextPeriodicAt: '2026-10-06T06:32:42.000Z' };
    await vi.dynamicImportSettled();
    finish({ ok: true, data: result }); await flushPromises();
    expect(wrapper.emitted('completed')).toEqual([[result]]); expect(wrapper.get('.countdown').text()).toBe('42 秒');
    expect(wrapper.get('button').attributes('disabled')).toBeUndefined();
    wrapper.unmount(); bridge.dispose();
  });
  it('keeps scan failures local and retries with the same operationId', async () => {
    const bridge = createValidatedBridge('preview', async () => ({ ok: false, error: { code: 'WRITE_TIMEOUT', message: '超时', retryable: true } }));
    const call = vi.spyOn(bridge, 'call');
    const wrapper = mount(ScanButton, { global: { provide: { [BridgeKey as symbol]: bridge } } });
    await wrapper.get('button').trigger('click'); await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain('写入等待超时'); expect(wrapper.emitted('completed')).toBeUndefined();
    await wrapper.get('button').trigger('click'); await flushPromises();
    expect(call.mock.calls[1]![1]).toEqual(call.mock.calls[0]![1]);
    wrapper.unmount(); bridge.dispose();
  });
  it('preview scans emit consistent events and reuse completed receipts', async () => {
    const bridge = createMockBridge();
    const tick = vi.fn(); const usage = vi.fn();
    bridge.events.subscribe('collection.tick', tick); bridge.events.subscribe('usage.changed', usage);
    const operationId = crypto.randomUUID();
    const first = await bridge.call('collection.scanNow', { operationId });
    expect(first.ok).toBe(true);
    if (first.ok) expect(tick.mock.calls[0]![0]).toMatchObject({ lastCompletedAt: first.data.completedAt, nextPeriodicAt: first.data.nextPeriodicAt });
    expect(await bridge.call('collection.scanNow', { operationId })).toEqual(first);
    expect(tick).toHaveBeenCalledTimes(1); expect(usage).toHaveBeenCalledTimes(1);
    const status = await bridge.call('collection.status', {}); expect(status.ok).toBe(true);
    bridge.dispose();
  });
});
