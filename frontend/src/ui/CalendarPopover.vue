<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import TimeList from './TimeList.vue';
import {
  addCalendarDays, addCalendarMonths, calendarDate, dateTimeParts, formatDateTimeParts, parseDateTimeParts,
  type CalendarDate, type TimeZoneMode,
} from '../shared/time';

const props = withDefaults(defineProps<{ modelValue?: string; min?: string; max?: string; mode?: TimeZoneMode; allowFollow?: boolean; following?: boolean }>(), {
  modelValue: '', min: '', max: '', mode: 'system',
});
const emit = defineEmits<{ select: [value: string]; clear: []; now: []; close: []; follow: [] }>();
const initial = parseDateTimeParts(props.modelValue) ?? { ...dateTimeParts(new Date(), props.mode), hour: 0, minute: 0 };
const cursor = ref<CalendarDate>(initial);
const hour = ref(initial.hour);
const minute = ref(initial.minute);
const grid = ref<HTMLElement>();
const weekdays = ['一', '二', '三', '四', '五', '六', '日'];
const dateKey = (date: CalendarDate) => formatDateTimeParts({ ...date, hour: 0, minute: 0 }).slice(0, 10);
const selected = computed(() => parseDateTimeParts(props.modelValue));
const today = computed(() => dateTimeParts(new Date(), props.mode));
const monthTitle = computed(() => `${cursor.value.year}年${cursor.value.month}月`);
function disabled(date: CalendarDate) {
  if (date.year < 1 || date.year > 9999) return true;
  const key = dateKey(date);
  return Boolean((props.min && key < props.min.slice(0, 10)) || (props.max && key > props.max.slice(0, 10)));
}
const cells = computed(() => {
  const first = { ...cursor.value, day: 1 };
  const offset = (calendarDate(first).getUTCDay() + 6) % 7;
  return Array.from({ length: 42 }, (_, i) => {
    const date = addCalendarDays(first, i - offset);
    const weekday = calendarDate(date).getUTCDay();
    return {
      ...date, key: dateKey(date), disabled: disabled(date), current: date.month === cursor.value.month,
      selected: Boolean(selected.value && dateKey(selected.value) === dateKey(date)),
      today: dateKey(today.value) === dateKey(date),
      label: `${date.year}年${date.month}月${date.day}日 星期${['日', '一', '二', '三', '四', '五', '六'][weekday]}`,
    };
  });
});
const weeks = computed(() => Array.from({ length: 6 }, (_, i) => cells.value.slice(i * 7, i * 7 + 7)));
const focusKey = computed(() => disabled(cursor.value) ? cells.value.find(cell => !cell.disabled)?.key : dateKey(cursor.value));
async function focusDay() {
  await nextTick();
  grid.value?.querySelector<HTMLButtonElement>(`[data-day="${focusKey.value}"]`)?.focus();
}
watch(() => props.modelValue, text => {
  const parts = parseDateTimeParts(text);
  if (parts) { cursor.value = parts; hour.value = parts.hour; minute.value = parts.minute; }
  else if (!text) { hour.value = 0; minute.value = 0; }
});
function move(date: CalendarDate) {
  if (disabled(date)) return;
  cursor.value = date;
  void focusDay();
}
function shiftMonth(months: number) {
  let date = addCalendarMonths(cursor.value, months);
  const min = parseDateTimeParts(props.min); const max = parseDateTimeParts(props.max);
  if (min && dateKey(date) < dateKey(min)) date = min;
  if (max && dateKey(date) > dateKey(max)) date = max;
  move(date);
}
function monthDisabled(months: number) {
  const date = addCalendarMonths({ ...cursor.value, day: 1 }, months);
  const last = addCalendarDays(addCalendarMonths(date, 1), -1);
  return Boolean((props.min && dateKey(last) < props.min.slice(0, 10)) || (props.max && dateKey(date) > props.max.slice(0, 10))) || date.year < 1 || date.year > 9999;
}
function select(date: CalendarDate) {
  if (disabled(date) || !Number.isInteger(hour.value) || hour.value < 0 || hour.value > 23 || !Number.isInteger(minute.value) || minute.value < 0 || minute.value > 59) return;
  cursor.value = date;
  emit('select', formatDateTimeParts({ ...date, hour: hour.value, minute: minute.value }));
}
function selectToday() {
  if (disabled(today.value)) return;
  cursor.value = today.value;
  select(today.value);
  void focusDay();
}
function updateTime() { select(selected.value ?? cursor.value); }
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); emit('close'); return; }
  if (!(event.target instanceof HTMLElement) || !event.target.dataset.day) return;
  const day = event.target.dataset.day;
  const cell = cells.value.find(item => item.key === day);
  if (!cell) return;
  const weekday = (calendarDate(cell).getUTCDay() + 6) % 7;
  const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -weekday, End: 6 - weekday }[event.key];
  if (delta !== undefined) { event.preventDefault(); move(addCalendarDays(cell, delta)); }
  else if (event.key === 'PageUp' || event.key === 'PageDown') {
    event.preventDefault(); move(addCalendarMonths(cell, (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1)));
  } else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(cell); }
}
onMounted(focusDay);
</script>

<template>
  <div class="calendar" role="dialog" aria-label="选择日期与时间" @keydown="onKeydown">
    <div class="calendar__header">
      <button type="button" aria-label="上个月" :disabled="following || monthDisabled(-1)" @click="shiftMonth(-1)">‹</button>
      <strong>{{ monthTitle }}</strong>
      <button type="button" aria-label="下个月" :disabled="following || monthDisabled(1)" @click="shiftMonth(1)">›</button>
    </div>
    <div ref="grid" role="grid" aria-label="日期" class="calendar__grid">
      <div role="row" class="calendar__row calendar__weekdays"><span v-for="day in weekdays" :key="day" role="columnheader">{{ day }}</span></div>
      <div v-for="(week, index) in weeks" :key="index" role="row" class="calendar__row">
        <button v-for="cell in week" :key="cell.key" :data-day="cell.key" class="calendar__day"
          :class="{ muted: !cell.current, selected: cell.selected, today: cell.today }" type="button" role="gridcell"
          :aria-label="cell.label" :aria-selected="cell.selected" :aria-current="cell.today ? 'date' : undefined"
          :disabled="following || cell.disabled" :tabindex="cell.key === focusKey ? 0 : -1" @click="select(cell)">
          {{ cell.day }}
        </button>
      </div>
    </div>
    <fieldset class="calendar__time" :disabled="following">
      <span>时</span><TimeList v-model="hour" :maximum="23" label="小时" @change="updateTime" />
      <span>分</span><TimeList v-model="minute" :maximum="59" label="分钟" @change="updateTime" />
    </fieldset>
    <div class="calendar__actions">
      <button type="button" :disabled="following" @click="$emit('clear')">清除</button>
      <button type="button" :disabled="following" @click="$emit('now')">当前时间</button>
      <button v-if="allowFollow" type="button" :aria-pressed="following" @click="$emit('follow')">随当前时间变动</button>
      <button v-else type="button" :disabled="disabled(today)" @click="selectToday">今天</button>
    </div>
  </div>
</template>

<style scoped>
.calendar { width: 300px; padding: 12px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 6px 18px rgb(0 0 0 / 15%); }
.calendar__header, .calendar__actions, .calendar__time { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.calendar__header button, .calendar__actions button { border: 0; background: transparent; padding: 6px 8px; }
.calendar__row { display: grid; grid-template-columns: repeat(7, 1fr); text-align: center; }
.calendar__weekdays { margin-top: 10px; color: var(--text-muted); font-size: 12px; }
.calendar__day { min-height: 34px; border: 0; background: transparent; border-radius: 6px; }
.calendar__day.muted { color: var(--text-faint); }
.calendar__day.selected { color: var(--accent-contrast); background: var(--accent); }
.calendar__day.today { box-shadow: inset 0 0 0 1px var(--accent); }
.calendar__day:disabled { opacity: .4; }
.calendar__time { justify-content: center; margin:10px 0 0;padding:0;border:0; }
.calendar__time input { width: 60px; min-height: 32px; padding: 4px; border-radius: 4px; }
.calendar__actions { margin-top: 10px; border-top: 1px solid var(--border); padding-top: 8px; }
</style>
