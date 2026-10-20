<script setup lang="ts">
import { computed } from 'vue';
import type { CommandData } from '../../app/ports';
import DataTable from '../../ui/DataTable.vue';
import Badge from '../../ui/Badge.vue';
import Money from '../../ui/Money.vue';
import { formatInteger } from '../../shared/format';
import type { TableColumn } from '../../ui/table';
import UsageFootnote from './UsageFootnote.vue';
import { usageColumns, type MetricRow } from './usageColumns';
import type { ModelRow } from './types';
const props = defineProps<{ models: readonly ModelRow[]; auxiliary?: readonly CommandData<'stats.auxiliaryCosts'>['models'][number][] }>();
type Row = ModelRow & MetricRow;
const rows = computed<Row[]>(() => props.models.map(row => ({ ...row })));
const columns: TableColumn<Row>[] = [
  { key: 'displayName', label: '模型', sortable: true },
  { key: 'recordCount', label: '记录数', sortable: true, numeric: true, format: (_, row) => formatInteger(row.recordCount) },
  ...usageColumns<Row>(true),
];
const coverageIncomplete = computed(() => props.models.some(row => row.cacheCoverageIncomplete));
</script>

<template>
  <section class="model-table" aria-label="模型明细">
    <h2>模型明细</h2>
    <DataTable :rows="rows" :columns="columns" row-key="modelKey" label="模型明细表" :initial-sort="{ key: 'picoUsd', direction: 'desc' }">
      <template #cell-displayName="{ row }"><span :title="row.displayName">{{ row.displayName }}</span><span class="model-badges"><Badge v-if="row.unknownModelCount" tone="warning">模型未知</Badge><Badge v-if="row.unpriced" tone="warning">未定价</Badge><Badge v-if="row.incomplete" tone="warning">不完整</Badge></span></template>
      <template #cell-picoUsd="{ row }"><Money :pico="row.picoUsd" /></template>
      <template #empty>当前范围暂无入账用量</template>
    </DataTable>
    <div v-if="auxiliary?.length" class="auxiliary"><h3>辅助调用</h3><div v-for="row in auxiliary" :key="row.modelId ?? 'unknown'"><strong>{{ row.modelId ?? '模型未知（旧记录未保存）' }}</strong> · {{ row.purpose }} · 用量未知 · {{ row.count }} 次 · <Money :pico="row.costPico" /><span class="text-muted">（固定费率估算，不计入普通模型排名或未定价模型）</span></div></div>
    <UsageFootnote :show="coverageIncomplete" />
  </section>
</template>

<style scoped>
.auxiliary{margin-top:16px;display:grid;gap:10px;font-size:13px}
h2 { margin-bottom: 12px; }.model-badges { display: inline-flex; gap: 4px; margin-left: 8px; }
</style>
