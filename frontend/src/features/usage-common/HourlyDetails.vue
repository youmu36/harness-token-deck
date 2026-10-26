<script setup lang="ts">
import { computed, inject, onScopeDispose, ref, watch } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { readShared } from '../../shared/readService';
import { useQuery } from '../../shared/query';
import type { ModelRow, UsageHarness, UsageRange } from './types';
import HourlyTable from './HourlyTable.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ harness: UsageHarness; range: UsageRange; queryKey: string; models: readonly ModelRow[]; modelValue: string | null; focusBucket?: string }>();
defineEmits<{ 'update:modelValue': [value: string | null] }>();
const bridge = inject(BridgeKey)!;
const opened = ref(false);
const day = 86400000;
const full = ref(false);
const loaded = ref(1);
const selectedDay = ref('');
const days = computed(() => {
  const offset = props.range.offsetMinutes * 60000;
  const first = Date.parse(props.range.start), end = Date.parse(props.range.end);
  const result: { value: string; label: string; start: number; end: number }[] = [];
  for (let at = Math.floor((first + offset) / day) * day - offset; at < end; at += day) {
    result.push({ value: String(at), label: new Date(at + offset).toISOString().slice(0, 10), start: Math.max(first, at), end: Math.min(end, at + day) });
  }
  return result;
});
const longRange = computed(() => Date.parse(props.range.end) - Date.parse(props.range.start) > 7 * day);
watch(() => props.queryKey, () => { loaded.value = 1; selectedDay.value = ''; full.value = false; });
watch(() => props.modelValue, () => { loaded.value = 1; });
const intervals = computed(() => {
  if (!longRange.value) return [{ start: Date.parse(props.range.start), end: Date.parse(props.range.end) }];
  if (full.value) return days.value.slice(0, loaded.value);
  const selected = days.value.find(item => item.value === selectedDay.value) ?? days.value.at(-1);
  return selected ? [selected] : [];
});
const requestKey = computed(() => [props.harness, props.modelValue, props.range.offsetMinutes, ...intervals.value.flatMap(i => [i.start, i.end])].join('|'));
const pages = new Map<string, CommandData<'stats.summary'>>();
let revisionGeneration = 0;
onScopeDispose(bridge.events.subscribe('usage.changed', event => {
  if (props.harness === 'all' || event.harnesses.includes(props.harness)) { revisionGeneration++; pages.clear(); }
}));
onScopeDispose(bridge.events.subscribe('cost.changed', () => {revisionGeneration++;pages.clear();}));
watch(opened, value => {if(!value)pages.clear();});
watch(() => props.focusBucket, value => {
  if (!value) return;
  const at = Date.parse(value), offset = props.range.offsetMinutes * 60000;
  selectedDay.value = String(Math.floor((at + offset) / day) * day - offset);
  full.value = false; opened.value = true;
});
const query = useQuery('usage.hours', async signal => {
  const rows = [];
  const generation = revisionGeneration;
  for (const interval of intervals.value) {
    const key = [props.harness, props.modelValue, props.range.offsetMinutes, interval.start, interval.end].join('|');
    const cached = pages.get(key);
    if (cached) { rows.push(cached); continue; }
    const result = await readShared(bridge, 'stats.summary', {
      harness: props.harness, start: new Date(interval.start).toISOString(), end: new Date(interval.end).toISOString(),
      offsetMinutes: props.range.offsetMinutes, grain: 'hour', sections: props.modelValue === null ? ['hourlyTotals'] : ['hourly'], modelKey: props.modelValue,
    }, signal);
    rows.push(result);
    if (generation === revisionGeneration) { pages.set(key, result); while (pages.size > 4) pages.delete(pages.keys().next().value!); }
  }
  const historyNotice = rows.find(row => row.historyNotice)?.historyNotice;
  return { hourly: historyNotice ? [] : rows.flatMap(row => row.hourly), hourlyTotals: historyNotice ? [] : rows.flatMap(row => row.hourlyTotals), historyNotice };
}, { visible: opened, deps: [requestKey], clearOnChange: true, clearOnHide: true,
  identity: () => [props.harness,props.queryKey,props.modelValue,props.range.offsetMinutes,selectedDay.value,full.value].join('|'),
  coalesce: true, eventIntervalMs:5000,
  events: ['cost.changed', { name: 'usage.changed', when: event => props.harness === 'all' || event.harnesses.includes(props.harness) }] });
</script>
<template>
  <section class="hourly-details">
    <Button :aria-expanded="opened" @click="opened = !opened">{{ opened ? '收起小时明细' : '查看小时明细' }}</Button>
    <template v-if="opened">
      <div v-if="longRange" class="detail-controls">
        <label><input v-model="full" type="checkbox" @change="loaded = 1" />查看完整区间（分段加载）</label>
        <label v-if="!full">选择日期 <select :value="selectedDay || days.at(-1)?.value" @change="selectedDay = ($event.target as HTMLSelectElement).value"><option v-for="item in days" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
        <span v-else>已加载 {{ Math.min(loaded, days.length) }} / {{ days.length }} 天</span>
      </div>
      <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
      <InlineAlert v-if="query.data.value?.historyNotice">{{ query.data.value.historyNotice }}</InlineAlert>
      <HourlyTable :models="models" :hourly="query.data.value?.hourly ?? []" :hourly-totals="query.data.value?.hourlyTotals ?? []" :model-value="modelValue" :loading="query.loading.value" @update:model-value="$emit('update:modelValue', $event)" />
      <Button v-if="longRange && full && loaded < days.length" :pending="query.loading.value || query.refreshing.value" @click="loaded++">加载下一天</Button>
    </template>
  </section>
</template>
<style scoped>
.hourly-details { display: grid; gap: 16px; }.hourly-details > button { justify-self: start; }.detail-controls { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; }select { padding: 6px; }
</style>
