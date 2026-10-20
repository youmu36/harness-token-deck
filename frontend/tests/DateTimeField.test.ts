import { mount } from '@vue/test-utils';
import { afterEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import DateTimeField from '../src/ui/DateTimeField.vue';
import { formatDateTimeInput } from '../src/shared/time';

const cleanup: (() => void)[] = [];
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers(); document.body.innerHTML = ''; });
function fixture(props = {}) {
  const wrapper = mount(DateTimeField, { attachTo: document.body, props: { mode: 'UTC', ...props } });
  cleanup.push(() => wrapper.unmount()); return wrapper;
}
const tick = async () => { await nextTick(); await nextTick(); };
it('commits text on Enter/blur and keeps invalid drafts with field errors', async () => {
  const wrapper = fixture();
  const input = wrapper.get('input');
  await input.setValue('2024-02-29 12:34'); await input.trigger('keydown', { key: 'Enter' });
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['2024-02-29 12:34']);
  await input.setValue('2026-02-29 12:34'); await input.trigger('blur');
  expect(wrapper.get('[role="alert"]').text()).toContain('有效');
  expect((input.element as HTMLInputElement).value).toBe('2026-02-29 12:34');
  expect(wrapper.emitted('update:modelValue')).toHaveLength(1);
  expect(wrapper.emitted('validity-change')?.at(-1)).toEqual([false]);
});
it('renders a 7×6 grid with Chinese labels and footer order; today retains hours/minutes, now closes, clear empties', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T12:34:59Z'));
  const wrapper = fixture({ modelValue: '2026-09-27 08:17' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  expect(wrapper.findAll('[role="gridcell"]')).toHaveLength(42);
  expect(wrapper.get('[aria-label="2026年9月27日 星期日"]').attributes('aria-selected')).toBe('true');
  const actions = wrapper.findAll('.calendar__actions button');
  expect(actions.map(button => button.text())).toEqual(['清除', '当前时间', '今天']);
  await actions[2]!.trigger('click'); await tick();
  expect(wrapper.get('.calendar__header strong').text()).toBe('2026年10月');
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['2026-10-06 08:17']);
  expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
  await wrapper.get('.calendar__actions button').trigger('click');
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['']);
  await wrapper.findAll('.calendar__actions button')[2]!.trigger('click');
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['2026-10-06 00:00']);
  await wrapper.findAll('.calendar__actions button')[1]!.trigger('click');
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['2026-10-06 12:34']);
  expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
  expect(document.activeElement?.getAttribute('aria-label')).toBe('打开日历');
});
it('today without a selected time uses midnight in system mode', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T12:34:59Z'));
  const wrapper = fixture({ mode: 'system' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  await wrapper.findAll('.calendar__actions button')[2]!.trigger('click');
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([formatDateTimeInput(new Date()).slice(0, 11) + '00:00']);
});
it('Esc restores calendar-button focus', async () => {
  const wrapper = fixture({ modelValue: '2026-10-06 12:34' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  expect(document.activeElement?.getAttribute('role')).toBe('gridcell');
  await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' });
  expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
  expect(document.activeElement?.getAttribute('aria-label')).toBe('打开日历');
});
it('minute inputs update independently without a global lock; outside clicks dismiss the popover', async () => {
  const wrapper = fixture({ modelValue: '2026-10-06 12:34' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  await wrapper.get('button[aria-label="小时"]').trigger('click'); await tick(); await wrapper.get('[role=option][data-value="23"]').trigger('click');
  await wrapper.get('button[aria-label="分钟"]').trigger('click'); await tick(); await wrapper.get('[role=option][data-value="59"]').trigger('click');
  expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['2026-10-06 23:59']);
  document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
  await tick();
  expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
});
it('month buttons reach a partially allowed month and clamp its focus date', async () => {
  const wrapper = fixture({ modelValue: '2026-10-31 12:34', max: '2026-11-02 12:34' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  expect(wrapper.get('[aria-label="下个月"]').attributes('disabled')).toBeUndefined();
  await wrapper.get('[aria-label="下个月"]').trigger('click'); await tick();
  expect(wrapper.get('.calendar__header strong').text()).toBe('2026年11月');
  expect(document.activeElement?.getAttribute('data-day')).toBe('2026-11-02');
  expect(wrapper.get('[aria-label="下个月"]').attributes('disabled')).toBeDefined();
});
it('all date shortcuts cross months/years, clamp leap days, and Enter selects', async () => {
  const wrapper = fixture({ modelValue: '2024-02-29 12:34' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  const key = async (key: string, shiftKey = false) => {
    const active = document.activeElement as HTMLElement;
    active.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey, bubbles: true, cancelable: true })); await tick();
    return document.activeElement?.getAttribute('data-day');
  };
  expect(await key('ArrowRight')).toBe('2024-03-01');
  expect(await key('ArrowLeft')).toBe('2024-02-29');
  expect(await key('ArrowUp')).toBe('2024-02-22');
  expect(await key('ArrowDown')).toBe('2024-02-29');
  expect(await key('Home')).toBe('2024-02-26');
  expect(await key('End')).toBe('2024-03-03');
  expect(await key('PageUp')).toBe('2024-02-03');
  expect(await key('PageDown')).toBe('2024-03-03');
  expect(await key('PageUp', true)).toBe('2023-03-03');
  expect(await key('PageDown', true)).toBe('2024-03-03');
  await key('Enter'); expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['2024-03-03 12:34']);
});
it('min/max disable dates and validate minute bounds; now obeys bounds', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-06T12:34Z'));
  const wrapper = fixture({ modelValue: '2026-10-06 12:00', min: '2026-10-05 12:00', max: '2026-10-07 12:00' });
  await wrapper.get('[aria-label="打开日历"]').trigger('click'); await tick();
  expect(wrapper.get('[data-day="2026-10-04"]').attributes('disabled')).toBeDefined();
  expect(wrapper.get('[data-day="2026-10-08"]').attributes('disabled')).toBeDefined();
  expect(wrapper.get('[data-day="2026-10-07"]').attributes('disabled')).toBeUndefined();
  await wrapper.get('input[type="text"]').setValue('2026-10-07 12:01');
  await wrapper.get('input[type="text"]').trigger('keydown', { key: 'Enter' });
  expect(wrapper.get('[role="alert"]').text()).toContain('不能晚于');
  await wrapper.setProps({ max: '2026-10-06 12:00' });
  await wrapper.findAll('.calendar__actions button')[1]!.trigger('click');
  expect(wrapper.get('[role="alert"]').text()).toContain('不能晚于');
  expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
});
