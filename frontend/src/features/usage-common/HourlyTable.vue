<script setup lang="ts">
import { computed, useId, watch } from 'vue';
import DataTable from '../../ui/DataTable.vue';
import Money from '../../ui/Money.vue';
import type { TableColumn } from '../../ui/table';
import UsageFootnote from './UsageFootnote.vue';
import { usageColumns, type MetricRow } from './usageColumns';
import type { ModelRow, Summary } from './types';
const props = defineProps<{ models: readonly ModelRow[]; hourly: readonly Summary['hourly'][number][]; hourlyTotals: readonly Summary['hourlyTotals'][number][]; modelValue: string | null; loading?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>();
const id = useId();
const selected = computed(() => props.modelValue !== null && props.models.some(model => model.modelKey === props.modelValue) ? props.modelValue : null);
watch([() => props.models, () => props.modelValue, () => props.loading], () => {
  if (!props.loading && props.modelValue !== null && selected.value === null) emit('update:modelValue', null);
}, { immediate: true });
type Row = MetricRow & { bucket: string; label: string; displayName: string };
const rows = computed<Row[]>(() => (selected.value === null ? props.hourlyTotals : props.hourly.filter(row => row.modelKey === selected.value))
  .map(row => ({ ...row.metrics, bucket: row.bucket, label: row.label, displayName: row.displayName }))
  .sort((a, b) => Date.parse(a.bucket) - Date.parse(b.bucket)));
const columns: TableColumn<Row>[] = [
  { key: 'label', label: '小时' }, { key: 'displayName', label: '模型' }, ...usageColumns<Row>(false),
];
const coverageIncomplete = computed(() => rows.value.some(row => row.cacheCoverageIncomplete));
function choose(event: Event) {
  // Option indices keep the unknown model's empty key distinct from 'all'.
  const index = Number((event.target as HTMLSelectElement).value);
  emit('update:modelValue', index < 0 ? null : props.models[index]!.modelKey);
}
const selectedIndex = computed(() => selected.value === null ? -1 : props.models.findIndex(model => model.modelKey === selected.value));
</script>

<template>
  <section aria-label="小时明细">
    <header><h2>小时明细</h2><label :for="`${id}-model`">模型 <select :id="`${id}-model`" aria-label="小时明细模型" :value="selectedIndex" @change="choose">
      <option value="-1">全部模型</option><option v-for="(model, index) in models" :key="model.modelKey" :value="index">{{ model.displayName }}</option>
    </select></label></header>
    <DataTable :rows="rows" :columns="columns" row-key="bucket" :reset-key="modelValue ?? 'all'" label="小时明细表" :height="360">
      <template #cell-picoUsd="{ row }"><Money :pico="row.picoUsd" /></template>
      <template #empty>当前范围暂无入账用量</template>
    </DataTable>
    <UsageFootnote :show="coverageIncomplete" />
  </section>
</template>

<style scoped>
header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
label { color: var(--text-muted); font-size: 12px; }select { padding: 5px 8px; margin-left: 6px; max-width: 260px; }
</style>
