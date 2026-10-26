import { computed, ref } from 'vue';
import { formatDateTimeInput, parseDateTimeInput, queryTimezone, type TimeZoneMode } from '../../shared/time';
import { calculateRange } from '../usage-common/useTimeRange';
import type { AnalysisView } from './analysisState';

export function usePageRange(view: AnalysisView, mode: () => TimeZoneMode) {
  const now = ref(new Date());
  const recent = computed(() => calculateRange(view.recent, mode(), now.value));
  function resolve(at: Date) {
    if (view.period === 'range') return calculateRange(view.recent, mode(), at);
    const today = formatDateTimeInput(at, mode()), currentYear = Number(today.slice(0, 4));
    const year = view.period === 'month' ? currentYear : view.year;
    const month = view.period === 'month' ? Number(today.slice(5, 7)) : view.period === 'quarter' ? (view.quarter - 1) * 3 + 1 : 1;
    const wall = (y: number, m: number) => `${y}-${String(m).padStart(2, '0')}-01 00:00`;
    const endMonth = month + (view.period === 'quarter' ? 3 : view.period === 'year' ? 12 : 1);
    const start = view.period === 'all' ? new Date('1971-01-01T00:00:00Z') : parseDateTimeInput(wall(year, month), mode());
    const end = view.period === 'all' ? at : new Date(Math.min(at.getTime(), parseDateTimeInput(wall(year + Math.floor((endMonth - 1) / 12), (endMonth - 1) % 12 + 1), mode()).getTime()));
    return { start: start.toISOString(), end: end.toISOString(), ...queryTimezone(start, end, mode()) };
  }
  const range = computed(() => resolve(now.value));
  function advance() { return resolve(new Date()); }
  const rangeKind = computed(() => view.period === 'range' ? view.recent.range === 'custom' ? 'custom' as const : 'recent' as const : view.period === 'years' ? 'year' as const : view.period);
  return { range, recent, advance, rangeKind };
}
