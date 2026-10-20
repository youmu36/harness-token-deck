import { mount, flushPromises } from '@vue/test-utils';
import { defineComponent, h } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BridgeKey } from '../src/app/ports';
import { unwrap } from '../src/app/errors';
import { createMockBridge } from '../src/infra/bridge/mock';
import { calculateRange, useTimeRange, validateCustomRange } from '../src/features/usage-common/useTimeRange';
import { defaultFilter } from '../src/features/usage-common/types';
import TimeRangeBar from '../src/features/usage-common/TimeRangeBar.vue';

afterEach(() => { vi.useRealTimers(); });
describe('usage ranges', () => {
  it('keeps rolling 24 hours distinct from today and advances to the exact refresh instant', () => {
    const now = new Date('2026-10-16T06:23:45.678Z');
    const value = calculateRange({ ...defaultFilter, range: '24hours' }, 'UTC', now);
    expect(value.start).toBe('2026-10-15T06:23:45.678Z');
    expect(value.end).toBe(now.toISOString());
    expect(Date.parse(value.end) - Date.parse(value.start)).toBe(86_400_000);
    expect(value.start).not.toBe(calculateRange(defaultFilter, 'UTC', now).start);
  });
  it('opens presets as a secondary menu with 24 hours between today and seven days, leaving custom beside it', async () => {
    const wrapper = mount(TimeRangeBar, { props: { modelValue: 'today', range: calculateRange(defaultFilter, 'UTC'), mode: 'UTC' }, attachTo: document.body });
    expect(wrapper.find('[role="radiogroup"]').exists()).toBe(false);
    expect(wrapper.get('[data-value="custom"]').isVisible()).toBe(true);
    const trigger = wrapper.get('[aria-label^="时间范围："]');
    await trigger.trigger('click');
    expect(wrapper.findAll('[role="radio"]').map(option => option.text())).toEqual(['今天', '近 24 小时', '近 7 天', '近 30 天']);
    await wrapper.get('[data-value="24hours"]').trigger('click');
    expect(wrapper.emitted('update:modelValue')).toEqual([['24hours']]);
    expect(wrapper.find('[role="radiogroup"]').exists()).toBe(false);
    expect(document.activeElement).toBe(trigger.element);
    await trigger.trigger('keydown', { key: 'ArrowDown' });
    expect(wrapper.find('[role="radiogroup"]').exists()).toBe(true);
    await trigger.trigger('keydown', { key: 'Escape' });
    expect(trigger.attributes('aria-expanded')).toBe('false');
    wrapper.unmount();
  });
  it.each(['system', 'UTC'] as const)('computes calendar-day presets in %s with the start offset and exact now', mode => {
    const now = mode === 'UTC' ? new Date('2026-10-06T14:32:45.678Z') : new Date(2026, 9, 6, 14, 32, 45, 678);
    for (const [preset, days] of [['today', 0], ['7days', 6], ['30days', 29]] as const) {
      const expected = new Date(now);
      if (mode === 'UTC') { expected.setUTCDate(expected.getUTCDate() - days); expected.setUTCHours(0, 0, 0, 0); }
      else { expected.setDate(expected.getDate() - days); expected.setHours(0, 0, 0, 0); }
      expect(calculateRange({ ...defaultFilter, range: preset }, mode, now)).toEqual({
        start: expected.toISOString(), end: now.toISOString(), offsetMinutes: mode === 'UTC' ? 0 : -expected.getTimezoneOffset(), crossesDst: false,
      });
    }
  });
  it.each([
    ['', '2026-10-06 14:32', '请选择精确到分钟的起止时间'],
    ['2026-10-06 12:00:01', '2026-10-06 14:32', '请选择精确到分钟的起止时间'],
    ['2026-02-30 12:00', '2026-10-06 14:32', '请选择精确到分钟的起止时间'],
    ['2026-10-06 14:32', '2026-10-06 14:32', '结束时间须晚于开始时间'],
    ['2026-10-06 14:32', '2026-10-06 14:31', '结束时间须晚于开始时间'],
    ['2026-09-06 14:32', '2026-10-06 14:33', '自定义范围不能超过 30 天'],
  ])('validates custom input %s to %s', (start, end, error) => {
    expect(validateCustomRange(start, end, 'UTC')).toEqual({ error });
  });
  it('allows exactly 30 days and emits UTC instants', () => {
    expect(validateCustomRange('2026-09-06 14:32', '2026-10-06 14:32', 'UTC')).toEqual({ range: {
      start: '2026-09-06T14:32:00.000Z', end: '2026-10-06T14:32:00.000Z', offsetMinutes: 0, crossesDst: false,
    } });
  });
  it('persists each page independently, advances presets, and freezes applied custom ranges', async () => {
    const bridge = createMockBridge();
    const initial = unwrap(await bridge.call('preferences.get', {}));
    let clock = new Date('2026-10-06T10:32:45Z');
    let time!: ReturnType<typeof useTimeRange>;
    const host = mount(defineComponent({ setup() { time = useTimeRange('codex', initial, () => clock); return () => h('div'); } }), { global: { provide: { [BridgeKey as symbol]: bridge } } });
    const calls = vi.spyOn(bridge, 'call');
    await time.select('custom');
    expect(calls.mock.calls.filter(([command]) => command === 'preferences.update')).toHaveLength(0);
    expect(time.range.value.end).toBe(clock.toISOString());
    await time.select('7days');
    await time.saveFilter({ metric: 'cost' });
    const codex = time.filter.value;
    host.unmount();
    const overviewInitial = unwrap(await bridge.call('preferences.get', {}));
    let overview!: ReturnType<typeof useTimeRange>;
    const other = mount(defineComponent({ setup() { overview = useTimeRange('overview', overviewInitial, () => clock); return () => h('div'); } }), { global: { provide: { [BridgeKey as symbol]: bridge } } });
    await overview.select('30days');
    const saved = unwrap(await bridge.call('preferences.get', {}));
    expect(saved.preferences.filters.codex).toEqual(codex);
    expect(saved.preferences.filters.overview?.range).toBe('30days');
    clock = new Date('2026-10-06T11:32:45Z');
    expect(overview.advance().end).toBe(clock.toISOString());
    const custom = validateCustomRange('2026-10-05 11:00', '2026-10-06 11:00', 'UTC').range!;
    await overview.applyCustom(custom);
    clock = new Date('2026-10-07T12:32Z');
    expect(overview.advance()).toEqual(calculateRange(overview.filter.value, 'system', clock));
    expect(overview.range.value.start).toBe(custom.start);
    expect(overview.range.value.end).toBe(custom.end);
    other.unmount();
    const restoredInitial = unwrap(await bridge.call('preferences.get', {}));
    const restored = mount(defineComponent({ setup() { time = useTimeRange('codex', restoredInitial, () => clock); return () => h('div'); } }), { global: { provide: { [BridgeKey as symbol]: bridge } } });
    expect(time.selection.value).toBe('7days'); expect(time.filter.value.metric).toBe('cost');
    restored.unmount(); bridge.dispose();
  });
  it('prefills custom once, applies only valid drafts and keeps the range label on unapplied edits', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T14:32:45Z'));
    const range = calculateRange(defaultFilter, 'UTC');
    const wrapper = mount(TimeRangeBar, { props: { modelValue: 'today', range, mode: 'UTC' } });
    await wrapper.get('[data-value="custom"]').trigger('click');
    expect(wrapper.emitted('update:modelValue')).toEqual([['custom']]);
    await wrapper.setProps({ modelValue: 'custom' });
    const inputs = wrapper.findAll('input');
    expect(inputs.map(input => input.element.value)).toEqual(['2026-10-05 14:32', '2026-10-06 14:32']);
    await inputs[0]!.setValue('invalid'); await wrapper.get('form').trigger('submit');
    expect(wrapper.text()).toContain('请选择精确到分钟的起止时间');
    expect(wrapper.emitted('apply')).toBeUndefined();
    await inputs[0]!.setValue('2026-10-06 14:32'); await wrapper.get('form').trigger('submit');
    expect(wrapper.text()).toContain('结束时间须晚于开始时间');
    await inputs[0]!.setValue('2026-09-06 14:31'); await wrapper.get('form').trigger('submit');
    expect(wrapper.text()).toContain('自定义范围不能超过 30 天');
    await inputs[0]!.setValue('2026-10-05 10:00'); await wrapper.get('form').trigger('submit');
    expect(wrapper.emitted('apply')?.[0]?.[0]).toMatchObject({ start: '2026-10-05T10:00:00.000Z', end: '2026-10-06T14:32:00.000Z' });
    expect(wrapper.get('.range-label').text()).toContain('10-06 00:00 至 10-06 14:32');
    await wrapper.setProps({ modelValue: 'today' }); await wrapper.setProps({ modelValue: 'custom' });
    expect(wrapper.findAll('input')[0]!.element.value).toBe('2026-10-05 10:00');
    wrapper.unmount(); await flushPromises();
  });
  it('preserves custom draft instants when timezone changes with the fields visible or hidden', async () => {
    const custom = { ...defaultFilter, range: 'custom' as const, start: '2026-10-05T10:00:00.000Z', end: '2026-10-06T10:00:00.000Z' };
    const range = calculateRange(custom, 'system');
    const wrapper = mount(TimeRangeBar, { props: { modelValue: 'custom', range, customStart: custom.start, customEnd: custom.end, mode: 'system' } });
    await wrapper.setProps({ mode: 'UTC' });
    expect(wrapper.findAll('input').map(input => input.element.value)).toEqual(['2026-10-05 10:00', '2026-10-06 10:00']);
    await wrapper.setProps({ modelValue: 'today' }); await wrapper.setProps({ mode: 'system' });
    await wrapper.setProps({ mode: 'UTC' }); await wrapper.setProps({ modelValue: 'custom' });
    expect(wrapper.findAll('input').map(input => input.element.value)).toEqual(['2026-10-05 10:00', '2026-10-06 10:00']);
    wrapper.unmount();
  });
});
