<script setup lang="ts">
import type { CommandData } from '../../app/ports';
import type { TableColumn } from '../../ui/table';
import DataTable from '../../ui/DataTable.vue';
import Money from '../../ui/Money.vue';
type Metrics = CommandData<'stats.explore'>['totals'];
type Row = Metrics & { id:string; name:string; harness?: string } & Record<string, unknown>;
defineProps<{ rows: Row[]; label: string }>();
const integer = (value: unknown) => BigInt(String(value)).toLocaleString('en-US');
const columns: TableColumn<Row>[] = [{key:'name',label:'名称',sortable:true},{key:'recordCount',label:'请求数',numeric:true,sortable:true},
  ...[['newInput','新增输入'],['cacheReadTokens','缓存命中'],['outputTokens','输出'],['totalTokens','总 Token']].map(([key,label]) => ({key:key!,label:label!,numeric:true,sortable:true,format:(value:unknown) => integer(value)})),
  {key:'cacheHitRate',label:'命中率',numeric:true,format:(_,row) => row.cacheHitRate == null ? '—' : `${(row.cacheHitRate * 100).toFixed(1)}%`},
  {key:'picoUsd',label:'本地有效成本',numeric:true,sortable:true}];
</script>
<template><section :aria-label="label"><DataTable :rows="rows" :columns="columns" row-key="id" :label="label" :initial-sort="{key:'picoUsd',direction:'desc'}">
  <template #cell-name="{row}"><a v-if="row.harness" :href="`#/harness/${row.harness}`">{{ row.name }}</a><span v-else>{{ row.name }}</span><small v-if="row.unpriced || row.incomplete || row.cacheCoverageIncomplete"> · {{ row.unpriced ? '未定价' : '覆盖不完整' }}</small></template>
  <template #cell-newInput="{row}">{{ row.cacheCoverageIncomplete ? '—' : integer(row.newInput) }}</template>
  <template #cell-picoUsd="{row}"><Money :pico="row.picoUsd" /></template><template #empty>当前筛选交集为零，可减少条件或扩大时间范围。</template>
</DataTable></section></template>
