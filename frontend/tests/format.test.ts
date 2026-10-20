import { expect, it } from 'vitest';
import { formatAxisCost, formatDateTime, formatInteger, formatPercent, formatTokenCount } from '../src/shared/format';

it('formats full token counts and card abbreviations without losing the tooltip value', () => {
  expect(formatInteger(123456)).toBe('123,456');
  expect(formatTokenCount(9999)).toBe('9,999');
  expect(formatTokenCount(123456)).toBe('12.35 万');
  expect(formatTokenCount(123456789)).toBe('1.23 亿');
  expect(formatTokenCount(10000)).toBe('1 万');
  expect(formatTokenCount(null)).toBe('--');
});
it('formats ratios, unknown values, UTC midnight and cost axes', () => {
  expect(formatPercent(0.456)).toBe('45.6%');
  expect(formatPercent(0)).toBe('0.0%');
  expect(formatPercent(null)).toBe('--');
  expect(formatDateTime('2026-10-06T00:00:00Z', 'UTC')).toBe('2026-10-06 00:00');
  expect(formatDateTime('invalid')).toBe('--');
  expect(['0', '120000000000', '1200000000000', '120000000000000'].map(formatAxisCost)).toEqual(['$0', '$0.12', '$1.2', '$120']);
});
