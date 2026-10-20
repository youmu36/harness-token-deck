import { describe, expect, it } from 'vitest';
import { addCalendarMonths, convertDateTime, formatDateTimeInput, parseDateTimeInput, parseDateTimeParts, rangePreset } from '../src/shared/time';

describe('minute wall-clock parsing', () => {
  it.each(['2026-02-29 12:00', '2024-02-30 12:00', '2026-13-01 00:00', '2026-01-01 24:00', '2026-01-01 00:60', '2026-01-01', '2026-01-01T12:00', ' 2026-01-01 12:00', '0000-01-01 00:00'])('rejects %s instead of normalizing', text => {
    expect(parseDateTimeParts(text)).toBeNull();
    expect(() => parseDateTimeInput(text, 'UTC')).toThrow(RangeError);
  });
  it.each(['2024-02-29 12:34', '0001-01-01 00:00', '0099-12-31 23:59', '9999-12-31 23:59'])('round-trips %s', text => {
    expect(formatDateTimeInput(parseDateTimeInput(text, 'UTC'), 'UTC')).toBe(text);
  });
  it('clamps month/year changes to valid days', () => {
    expect(addCalendarMonths({ year: 2024, month: 1, day: 31 }, 1)).toEqual({ year: 2024, month: 2, day: 29 });
    expect(addCalendarMonths({ year: 2024, month: 2, day: 29 }, 12)).toEqual({ year: 2025, month: 2, day: 28 });
    expect(addCalendarMonths({ year: 2026, month: 12, day: 31 }, 1)).toEqual({ year: 2027, month: 1, day: 31 });
  });
  it('converts system/UTC while preserving the instant', () => {
    const text = '2026-10-06 12:34';
    const utc = convertDateTime(text, 'system', 'UTC');
    expect(convertDateTime(utc, 'UTC', 'system')).toBe(text);
  });
});
it('presets mean today plus previous 6/29 calendar days, ending at now', () => {
  const now = new Date('2026-10-06T12:34:56Z');
  for (const [preset, start] of [['today', '2026-10-06T00:00:00Z'], ['7days', '2026-09-30T00:00:00Z'], ['30days', '2026-09-07T00:00:00Z']] as const) {
    const range = rangePreset(preset, now, 'UTC');
    expect(range.start.toISOString()).toBe(new Date(start).toISOString());
    expect(range.end).toEqual(now); expect(range.offsetMinutes).toBe(0); expect(range.crossesDst).toBe(false);
  }
});
