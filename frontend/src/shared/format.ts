import { formatMoney } from './money';
import { formatDateTimeInput, type TimeZoneMode } from './time';

export function formatInteger(value: number | bigint | null | undefined): string {
  return value === null || value === undefined ? '--' : value.toLocaleString('en-US');
}
export function formatTokenCount(value: number | bigint | null | undefined): string {
  if (value === null || value === undefined) return '--';
  const n = BigInt(value); const absolute = n < 0n ? -n : n;
  const divisor = absolute >= 100_000_000n ? 100_000_000n : absolute >= 10_000n ? 10_000n : 0n;
  if (!divisor) return formatInteger(n);
  const rounded = (absolute * 100n + divisor / 2n) / divisor;
  const fraction = String(rounded % 100n).padStart(2, '0').replace(/0+$/, '');
  return `${n < 0n ? '-' : ''}${rounded / 100n}${fraction ? `.${fraction}` : ''} ${divisor === 10_000n ? '万' : '亿'}`;
}
/** Statistics sends a ratio (0..1), not a percentage. */
export function formatPercent(ratio: number | null | undefined): string {
  return ratio === null || ratio === undefined || !Number.isFinite(ratio) ? '--' : `${(ratio * 100).toFixed(1)}%`;
}
export function formatDateTime(value: Date | string | null | undefined, mode: TimeZoneMode = 'system'): string {
  if (value === null || value === undefined) return '--';
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? '--' : formatDateTimeInput(date, mode);
}
export { formatAxisCost } from './money';
export { formatMoney };
