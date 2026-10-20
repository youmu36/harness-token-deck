import { computed, ref } from 'vue';
import type { CommandData } from '../../app/ports';
import { useShellPreferences } from '../../app/useShellPreferences';
import { parseDateTimeInput, queryTimezone, rangePreset, type TimeZoneMode } from '../../shared/time';
import { defaultFilter, type RangePreset, type UsageFilter, type UsagePage, type UsageRange } from './types';

export function calculateRange(filter: UsageFilter, mode: TimeZoneMode, now = new Date()): UsageRange {
  const result = filter.range === 'custom'
    ? { start: filter.startNow ? now : new Date(filter.start!), end: filter.endNow ? now : new Date(filter.end!) }
    : rangePreset(filter.range, now, mode);
  return { start: result.start.toISOString(), end: result.end.toISOString(), ...queryTimezone(result.start, result.end, mode) };
}

export function validateCustomRange(startText: string, endText: string, mode: TimeZoneMode):
  { error: string; range?: undefined } | { error?: undefined; range: UsageRange } {
  let start: Date; let end: Date;
  try { start = parseDateTimeInput(startText, mode); end = parseDateTimeInput(endText, mode); }
  catch { return { error: '请选择精确到分钟的起止时间' }; }
  if (end <= start) return { error: '结束时间须晚于开始时间' };
  if (end.getTime() - start.getTime() > 30 * 86_400_000) return { error: '自定义范围不能超过 30 天' };
  return { range: { start: start.toISOString(), end: end.toISOString(), ...queryTimezone(start, end, mode) } };
}

/** Presets advance only when the page refreshes; custom windows retain their exact instants. */
export function useTimeRange(page: UsagePage, initial: CommandData<'preferences.get'>, clock = () => new Date(), override?: Partial<UsageFilter>) {
  const preferences = useShellPreferences(initial);
  const filter = ref<UsageFilter>({ ...defaultFilter, ...initial.preferences.filters[page], ...override, ...(page === 'overview' ? { startNow: false, endNow: false } : {}) });
  const selection = ref<RangePreset>(filter.value.range);
  const now = ref(clock());
  const mode = computed(() => preferences.snapshot.value.preferences.timezone);
  const range = computed(() => calculateRange(filter.value, mode.value, now.value));
  // A stable scalar prevents display-only edits (metric/model/monitor mode)
  // from starting a new rolling-window aggregation when the filter object changes.
  const span = computed(() => Date.parse(range.value.end) - Date.parse(range.value.start));
  const dailyOnly = computed(() => span.value > 7 * 86_400_000);
  const trendGrain = computed(() => dailyOnly.value ? 'day' as const : filter.value.grain);
  const queryGrain = computed(() => page === 'overview' && trendGrain.value === 'hour' && span.value >= 72 * 3600000 ? 'twoHours' as const : trendGrain.value);
  const queryKey = computed(() => [filter.value.harness, filter.value.range, filter.value.start,
    filter.value.end, filter.value.startNow, filter.value.endNow, queryGrain.value, mode.value].join('|'));
  async function saveFilter(patch: Partial<UsageFilter>) {
    filter.value = { ...filter.value, ...patch };
    await preferences.save({ filters: { [page]: { ...filter.value } } });
  }
  function advance() { now.value = clock(); return range.value; }
  async function select(preset: RangePreset) {
    selection.value = preset;
    if (preset === 'custom') return;
    advance();
    await saveFilter({ range: preset });
  }
  async function applyCustom(value: UsageRange) {
    selection.value = 'custom';
    await saveFilter({ range: 'custom', start: value.start, end: value.end, startNow: page !== 'overview' && !!value.startNow, endNow: page !== 'overview' && !!value.endNow });
  }
  return { filter, selection, mode, range, trendGrain, queryGrain, dailyOnly, queryKey, advance, select, applyCustom, saveFilter,
    error: preferences.error, retry: preferences.retry };
}
