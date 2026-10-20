import { mount } from '@vue/test-utils';
import { expect, it } from 'vitest';
import DateTimeField from '../../src/ui/DateTimeField.vue';
import { convertDateTime, detectLocalTime, parseDateTimeInput, rangePreset } from '../../src/shared/time';

it('runs in a real America/New_York process timezone', () => {
  expect(process.env.TZ).toBe('America/New_York');
  expect(new Date('2026-01-01T00:00Z').getTimezoneOffset()).toBe(300);
  expect(new Date('2026-07-01T00:00Z').getTimezoneOffset()).toBe(240);
});
it.each([
  ['2026-03-08 02:30', 'nonexistent', '不存在'], ['2026-11-01 01:30', 'ambiguous', '出现两次'],
])('rejects DST %s in helper, text field and calendar time selection', async (text, issue, message) => {
  expect(detectLocalTime(text, 'system')).toBe(issue);
  expect(() => parseDateTimeInput(text, 'system')).toThrow(message);
  const wrapper = mount(DateTimeField, { props: { mode: 'system' } });
  await wrapper.get('input').setValue(text);
  await wrapper.get('input').trigger('keydown', { key: 'Enter' });
  expect(wrapper.get('[role="alert"]').text()).toContain(message);
  expect(wrapper.emitted('update:modelValue')).toBeUndefined();
  await wrapper.get('[aria-label="打开日历"]').trigger('click');
  await wrapper.get('button[aria-label="分钟"]').trigger('click');
  await wrapper.get(`[role=option][data-value="${Number(text.slice(-2))}"]`).trigger('click');
  expect(wrapper.get('[role="alert"]').text()).toContain(message);
  wrapper.unmount();
  expect(detectLocalTime(text, 'UTC')).toBeNull();
});
it('keeps exact instants on conversion, calendar-day presets and the start-offset DST notice', () => {
  expect(convertDateTime('2026-07-01 12:30', 'system', 'UTC')).toBe('2026-07-01 16:30');
  expect(parseDateTimeInput('2026-03-08 03:30').toISOString()).toBe('2026-03-08T07:30:00.000Z');
  const range = rangePreset('7days', new Date('2026-03-09T16:30Z'), 'system');
  expect(range.start.toISOString()).toBe('2026-03-03T05:00:00.000Z');
  expect(range.end.toISOString()).toBe('2026-03-09T16:30:00.000Z');
  expect(range.offsetMinutes).toBe(-300);
  expect(range.crossesDst).toBe(true);
});
