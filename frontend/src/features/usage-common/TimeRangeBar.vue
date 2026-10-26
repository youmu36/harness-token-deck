<script setup lang="ts">
import { ref, useId, watch } from 'vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import DateTimeField from '../../ui/DateTimeField.vue';
import Button from '../../ui/Button.vue';
import Badge from '../../ui/Badge.vue';
import { convertDateTime, formatDateTimeInput, type TimeZoneMode } from '../../shared/time';
import { validateCustomRange } from './useTimeRange';
import type { RangePreset, UsageRange } from './types';

const props = defineProps<{ modelValue: RangePreset; range: UsageRange; mode: TimeZoneMode; customStart?: string | null; customEnd?: string | null; allowFollow?: boolean; startNow?: boolean; endNow?: boolean; draftStart?: string; draftEnd?: string; revealMenus?:boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: RangePreset]; apply: [range: UsageRange]; 'update:draftStart': [value:string]; 'update:draftEnd': [value:string] }>();
const id = useId();
const start = ref(props.draftStart ?? ''); const end = ref(props.draftEnd ?? ''); const error = ref('');
watch(start, value => emit('update:draftStart', value)); watch(end, value => emit('update:draftEnd', value));
const endNow = ref(!!props.endNow);
watch(() => props.endNow, value => { endNow.value = !!value; });
watch(() => props.range.end, value => {
  if (endNow.value) end.value = formatDateTimeInput(new Date(value), props.mode);
});
const startField = ref<InstanceType<typeof DateTimeField>>();
const endField = ref<InstanceType<typeof DateTimeField>>();
watch(() => props.modelValue, preset => {
  error.value = '';
  if (preset !== 'custom' || start.value || end.value) return;
  const now = new Date(); now.setSeconds(0, 0);
  start.value = formatDateTimeInput(props.customStart ? new Date(props.customStart) : new Date(now.getTime() - 86_400_000), props.mode);
  end.value = formatDateTimeInput(props.endNow ? new Date(props.range.end) : props.customEnd ? new Date(props.customEnd) : now, props.mode);
}, { immediate: true });
watch(() => props.mode, (mode, previous) => {
  // Mounted DateTimeFields convert themselves; hidden drafts still need to retain their instants.
  if (props.modelValue === 'custom') return;
  try { if (start.value) start.value = convertDateTime(start.value, previous, mode); } catch { /* Preserve invalid drafts. */ }
  try { if (end.value) end.value = convertDateTime(end.value, previous, mode); } catch { /* Preserve invalid drafts. */ }
});
function apply() {
  const startValid = startField.value?.commit(); const endValid = endField.value?.commit();
  if (!startValid || !endValid) { error.value = '请选择精确到分钟的起止时间'; return; }
  const result = validateCustomRange(start.value, end.value, props.mode);
  error.value = result.error ?? '';
  if (result.range) emit('apply', { ...result.range, startNow: false, endNow: endNow.value });
}
function followEnd(value: boolean) {
  endNow.value = value;
  // The toggle applies immediately, so navigation cannot discard an enabled clock.
  const result = validateCustomRange(start.value, end.value, props.mode);
  error.value = result.error ?? '';
  if (result.range) emit('apply', { ...result.range, startNow: false, endNow: value });
}
const shortTime = (value: string) => formatDateTimeInput(new Date(value), props.mode).slice(5);
const timezone = () => props.mode === 'UTC' ? 'UTC' : Intl.DateTimeFormat().resolvedOptions().timeZone;
</script>

<template>
  <div class="time-range-bar">
    <div class="range-tools">
      <div class="range-selectors"><slot name="source" />
        <ChoiceMenu :reveal-on-open="revealMenus" :model-value="modelValue" label="时间范围" placeholder="常用范围" :options="[
          { value: 'today', label: '今天' }, { value: '24hours', label: '近 24 小时' },
          { value: '7days', label: '近 7 天' }, { value: '30days', label: '近 30 天' },
        ]" @update:model-value="emit('update:modelValue', $event as RangePreset)" />
        <Button data-value="custom" :aria-pressed="modelValue === 'custom'" @click="emit('update:modelValue', 'custom')">自定义</Button>
      </div>
      <slot />
    </div>
    <form v-if="modelValue === 'custom'" class="custom-range" @submit.prevent="apply">
      <div><label :for="`${id}-start`">开始</label><DateTimeField :id="`${id}-start`" ref="startField" v-model="start" label="开始时间" :mode="mode" /></div>
      <div><label :for="`${id}-end`">结束</label><DateTimeField :id="`${id}-end`" ref="endField" v-model="end" :following="endNow" :allow-follow="allowFollow" label="结束时间" :mode="mode" @update:following="followEnd" /></div>
      <Button type="submit">应用</Button>
    </form>
    <p v-if="error" class="range-error" role="alert">{{ error }}</p>
    <div class="range-label text-muted"><span>{{ shortTime(range.start) }} 至 {{ shortTime(range.end) }}</span><Badge class="timezone">{{ timezone() }}</Badge>
      <span v-if="range.crossesDst">跨夏令时，按起点偏移分桶</span>
    </div>
  </div>
</template>

<style scoped>
.time-range-bar { display: grid; gap: 20px; }
.range-tools, .range-selectors, .custom-range, .range-label { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.range-tools { justify-content: space-between; }
.range-selectors > .button { min-height: 36px; }.range-selectors > .button[aria-pressed='true'] { background: var(--accent-soft); border-color: var(--accent); }
.custom-range { align-items: start; }
.custom-range > .button { margin-top: 23px; }
.custom-range label { display: block; margin-bottom: 4px; color: var(--text-muted); font-size: 12px; }
.range-label, .range-error { font-size: 12px; }
.range-label .timezone { margin-left: auto; background: transparent; padding-right: 0; }
.range-error { color: var(--danger); }
</style>
