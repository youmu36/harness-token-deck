import { expect, it } from 'vitest';
import { calculateRange, validateCustomRange } from '../../src/features/usage-common/useTimeRange';
import { defaultFilter } from '../../src/features/usage-common/types';

it.each(['2026-03-08T17:30Z', '2026-11-01T17:30Z'])('keeps exactly 24 elapsed hours across DST at %s', instant => {
  const now = new Date(instant);
  const value = calculateRange({ ...defaultFilter, range: '24hours' }, 'system', now);
  expect(Date.parse(value.end) - Date.parse(value.start)).toBe(86_400_000);
  expect(value.crossesDst).toBe(true);
  expect(value.offsetMinutes).toBe(-new Date(value.start).getTimezoneOffset());
});

it('keeps 30 calendar days across the fall DST change within the backend limit', () => {
  const range = calculateRange({ ...defaultFilter, range: '30days' }, 'system', new Date('2026-11-02T17:30Z'));
  expect(range.start).toBe('2026-10-04T04:00:00.000Z');
  expect(range.end).toBe('2026-11-02T17:30:00.000Z');
  expect(range.offsetMinutes).toBe(-240); expect(range.crossesDst).toBe(true);
  expect(Date.parse(range.end) - Date.parse(range.start)).toBeLessThanOrEqual(30 * 86_400_000);
});
it('validates custom DST gaps and folds and converts UTC independently', () => {
  expect(validateCustomRange('2026-03-08 02:30', '2026-03-08 04:00', 'system').error).toBe('请选择精确到分钟的起止时间');
  expect(validateCustomRange('2026-11-01 01:30', '2026-11-01 04:00', 'system').error).toBe('请选择精确到分钟的起止时间');
  expect(validateCustomRange('2026-11-01 01:30', '2026-11-01 04:00', 'UTC').range?.offsetMinutes).toBe(0);
  const range = validateCustomRange('2026-10-31 12:00', '2026-11-02 12:00', 'system').range!;
  expect(range.offsetMinutes).toBe(-240); expect(range.crossesDst).toBe(true);
});
