<script setup lang="ts">
import { computed } from 'vue';
import type { Summary } from '../usage-common/types';
import { usageColumns, type MetricRow } from '../usage-common/usageColumns';
import { formatInteger } from '../../shared/format';
import type { TableColumn } from '../../ui/table';
import DataTable from '../../ui/DataTable.vue';
import Money from '../../ui/Money.vue';
import UsageFootnote from '../usage-common/UsageFootnote.vue';
const props = defineProps<{ harnesses: Summary['harnesses'] }>();
type Row = Summary['harnesses'][number] & MetricRow;
const rows = computed<Row[]>(() => props.harnesses.map(row => ({ ...row })));
const columns: TableColumn<Row>[] = [
  { key: 'displayName', label: '来源', sortable: true },
  { key: 'recordCount', label: '记录数', sortable: true, numeric: true, format: (_, row) => formatInteger(row.recordCount) },
  ...usageColumns<Row>(true),
];
</script>

<template>
  <section aria-label="按来源汇总">
    <h2>按来源汇总</h2>
    <DataTable :rows="rows" :columns="columns" row-key="harness" label="按来源汇总表" :initial-sort="{ key: 'picoUsd', direction: 'desc' }">
      <template #cell-displayName="{ row }"><a :href="`#/harness/${row.harness}`">{{ row.displayName }}</a></template>
      <template #cell-picoUsd="{ row }"><Money :pico="row.picoUsd" /></template>
      <template #empty>当前范围暂无入账用量</template>
    </DataTable>
    <UsageFootnote :show="harnesses.some(row => row.cacheCoverageIncomplete)" />
  </section>
</template>

<style scoped>
h2 { margin-bottom: 12px; }.source-note { margin-top: 10px; font-size: 12px; }
</style>
