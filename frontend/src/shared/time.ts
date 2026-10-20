export type TimeZoneMode = 'system' | 'UTC';
export type CalendarDate = { year: number; month: number; day: number };
export type DateTimeParts = CalendarDate & { hour: number; minute: number };
export type DateTimeError = 'invalid' | 'nonexistent' | 'ambiguous';
const INPUT = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/;
const pad = (n: number, digits = 2) => String(n).padStart(digits, '0');

/** UTC is only a carrier for calendar arithmetic, including years 0001–0099. */
export function calendarDate(value: CalendarDate): Date {
  const date = new Date(0);
  date.setUTCFullYear(value.year, value.month - 1, value.day);
  date.setUTCHours(0, 0, 0, 0);
  return date;
}
export function calendarParts(date: Date): CalendarDate {
  return { year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate() };
}
export function addCalendarDays(value: CalendarDate, days: number): CalendarDate {
  const date = calendarDate(value);
  date.setUTCDate(date.getUTCDate() + days);
  return calendarParts(date);
}
export function addCalendarMonths(value: CalendarDate, months: number): CalendarDate {
  const date = calendarDate({ ...value, day: 1, month: value.month + months });
  const last = calendarDate({ ...calendarParts(date), month: date.getUTCMonth() + 2, day: 0 });
  return { ...calendarParts(date), day: Math.min(value.day, last.getUTCDate()) };
}
export function formatDateTimeParts(parts: DateTimeParts): string {
  return `${pad(parts.year, 4)}-${pad(parts.month)}-${pad(parts.day)} ${pad(parts.hour)}:${pad(parts.minute)}`;
}
export function dateTimeParts(date: Date, mode: TimeZoneMode): DateTimeParts {
  return mode === 'UTC'
    ? { ...calendarParts(date), hour: date.getUTCHours(), minute: date.getUTCMinutes() }
    : { year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(), hour: date.getHours(), minute: date.getMinutes() };
}
export function parseDateTimeParts(text: string): DateTimeParts | null {
  const match = INPUT.exec(text);
  if (!match) return null;
  const [, y, m, d, h, min] = match;
  const parts = { year: Number(y), month: Number(m), day: Number(d), hour: Number(h), minute: Number(min) };
  const probe = calendarParts(calendarDate(parts));
  return parts.year >= 1 && parts.year <= 9999 && parts.month === probe.month && parts.day === probe.day && parts.year === probe.year && parts.hour <= 23 && parts.minute <= 59 ? parts : null;
}
function sameParts(a: DateTimeParts, b: DateTimeParts): boolean {
  return a.year === b.year && a.month === b.month && a.day === b.day && a.hour === b.hour && a.minute === b.minute;
}
export function matchingInstants(parts: DateTimeParts, mode: TimeZoneMode = 'system'): Date[] {
  const nominal = calendarDate(parts);
  nominal.setUTCHours(parts.hour, parts.minute);
  if (mode === 'UTC') return [nominal];
  // Try both offsets around a transition and round-trip each candidate.
  // A local Date constructor would silently normalize gaps and pick one fold.
  const offsets = new Set<number>();
  for (let hours = -48; hours <= 48; hours += 6) offsets.add(new Date(nominal.getTime() + hours * 3_600_000).getTimezoneOffset());
  return [...offsets].map(offset => new Date(nominal.getTime() + offset * 60_000))
    .filter(date => sameParts(dateTimeParts(date, 'system'), parts)).sort((a, b) => a.getTime() - b.getTime());
}
export function detectLocalTime(text: string, mode: TimeZoneMode = 'system'): DateTimeError | null {
  const parts = parseDateTimeParts(text);
  if (!parts) return 'invalid';
  const count = matchingInstants(parts, mode).length;
  return count === 0 ? 'nonexistent' : count > 1 ? 'ambiguous' : null;
}
export function parseDateTimeInput(text: string, mode: TimeZoneMode = 'system'): Date {
  const parts = parseDateTimeParts(text);
  if (!parts) throw new RangeError('请输入有效的 YYYY-MM-DD HH:mm 时间');
  const matches = matchingInstants(parts, mode);
  if (!matches.length) throw new RangeError('该本地时间因夏令时不存在，请调整或改用 UTC');
  if (matches.length > 1) throw new RangeError('该本地时间因夏令时出现两次，请调整或改用 UTC');
  return matches[0]!;
}
export function formatDateTimeInput(value: Date, mode: TimeZoneMode = 'system'): string {
  return formatDateTimeParts(dateTimeParts(value, mode));
}
/** Conversion preserves the instant; only its wall-clock representation changes. */
export function convertDateTime(text: string, from: TimeZoneMode, to: TimeZoneMode): string;
export function convertDateTime(value: Date, from: TimeZoneMode, to: TimeZoneMode): Date;
export function convertDateTime(value: string | Date, from: TimeZoneMode, to: TimeZoneMode): string | Date {
  if (value instanceof Date) return new Date(value.getTime());
  return formatDateTimeInput(parseDateTimeInput(value, from), to);
}
export function queryTimezone(start: Date, end: Date, mode: TimeZoneMode): { offsetMinutes: number; crossesDst: boolean } {
  return { offsetMinutes: mode === 'UTC' ? 0 : -start.getTimezoneOffset(), crossesDst: mode === 'system' && start.getTimezoneOffset() !== new Date(end.getTime() - 1).getTimezoneOffset() };
}
export type DateRangePreset = 'today' | '24hours' | '7days' | '30days';
export function rangePreset(preset: DateRangePreset, now = new Date(), mode: TimeZoneMode = 'system'): { start: Date; end: Date; offsetMinutes: number; crossesDst: boolean } {
  if (preset === '24hours') {
    const start = new Date(now.getTime() - 86_400_000); const end = new Date(now);
    return { start, end, ...queryTimezone(start, end, mode) };
  }
  const days = preset === 'today' ? 1 : preset === '7days' ? 7 : 30;
  const startParts = { ...addCalendarDays(dateTimeParts(now, mode), -(days - 1)), hour: 0, minute: 0 };
  const start = parseDateTimeInput(formatDateTimeParts(startParts), mode);
  const end = new Date(now);
  return { start, end, ...queryTimezone(start, end, mode) };
}

export const parseDateTime = parseDateTimeInput;
export const formatDateTime = formatDateTimeInput;
