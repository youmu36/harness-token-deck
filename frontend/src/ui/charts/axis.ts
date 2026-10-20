import { formatAxisCost, formatMoney } from '../../shared/money';

export type AxisFormat = 'tokens' | 'cost' | 'percent' | ((value: number) => string);
export interface ChartSegment { label: string; value: number | null; color?: string }
export interface ChartPoint {
  label: string;
  tooltipLabel?: string;
  value: number | null;
  /** Exact display text, e.g. formatted pico USD, separate from SVG coordinates. */
  valueLabel?: string;
  segments?: readonly ChartSegment[];
  flags?: { cacheBust?: boolean; cacheLoss?: boolean; contextCompacted?: boolean };
}

export function formatTick(value: number, format: AxisFormat): string {
  if (typeof format === 'function') return format(value);
  if (format === 'percent') return `${Number((value * 100).toFixed(1))}%`;
  // Cost coordinates are pico USD; only the money module formats them.
  if (format === 'cost') return formatAxisCost(BigInt(Math.round(value)));
  const absolute = Math.abs(value);
  const divisor = absolute >= 1e8 ? 1e8 : absolute >= 1e4 ? 1e4 : 1;
  return `${Number((value / divisor).toPrecision(4)).toLocaleString('en-US')}${divisor === 1e8 ? '亿' : divisor === 1e4 ? '万' : ''}`;
}

/** Tooltips and the reader table retain full values instead of axis abbreviations. */
export function formatValue(value: number | null, format: AxisFormat): string {
  if (value === null) return '--（未知）';
  if (typeof format === 'function') return format(value);
  if (format === 'percent') return `${(value * 100).toFixed(1)}%`;
  if (format === 'cost') return formatMoney(BigInt(Math.round(value)), 'usd4');
  return value.toLocaleString('en-US');
}

export function pointBounds(point: ChartPoint): [number, number] {
  if (point.value === null) return [0, 0];
  if (!point.segments?.length) return [Math.min(0, point.value), Math.max(0, point.value)];
  return point.segments.reduce<[number, number]>((bounds, segment) => {
    if (segment.value !== null) bounds[segment.value < 0 ? 0 : 1] += segment.value;
    return bounds;
  }, [0, 0]);
}

export function createAxis(points: readonly ChartPoint[], format: AxisFormat, count = 5) {
  const bounds = points.map(pointBounds);
  let min = Math.min(0, ...bounds.map(b => b[0]));
  let max = Math.max(0, ...bounds.map(b => b[1]));
  if (format === 'percent') { min = 0; max = 1; }
  if (min === max) max = 1;
  const rough = (max - min) / Math.max(1, count - 1);
  const power = 10 ** Math.floor(Math.log10(rough));
  const fraction = rough / power;
  const step = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10) * power;
  min = Math.floor(min / step) * step;
  max = Math.ceil(max / step) * step;
  const ticks = Array.from({ length: Math.round((max - min) / step) + 1 }, (_, i) => Number((min + i * step).toPrecision(12)));
  return { min, max, ticks };
}

export function tickIndices(length: number, count: number): number[] {
  if (!length) return [];
  const size = Math.min(length, Math.max(1, Math.floor(count)));
  if (size === 1) return [0];
  return Array.from({ length: size }, (_, i) => Math.round(i * (length - 1) / (size - 1)));
}

export function warningLabels(point: ChartPoint): string[] {
  const labels = point.flags?.cacheBust ? ['缓存击穿'] : point.flags?.cacheLoss ? ['大量未命中'] : [];
  if (point.flags?.contextCompacted) labels.push('上下文压缩');
  return labels;
}
