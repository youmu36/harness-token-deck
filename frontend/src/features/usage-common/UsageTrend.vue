<script setup lang="ts">
import { computed } from 'vue';
import SegmentedControl from '../../ui/SegmentedControl.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import { formatMoney } from '../../shared/money';
import type { ChartPoint } from '../../ui/charts/axis';
import type { Summary, UsageFilter } from './types';
const props = defineProps<{ summary?: Summary; start: string; end: string; metric: UsageFilter['metric']; grain: UsageFilter['grain']; dailyOnly?: boolean }>();
const emit = defineEmits<{ 'update:metric': [value: UsageFilter['metric']]; 'update:grain': [value: UsageFilter['grain']]; selectDay: [bucket: string] }>();
const actualGrain = computed(() => props.summary?.grain ?? props.grain);
const points = computed<ChartPoint[]>(() => props.summary?.trend.map(point => {
  const tooltipLabel = actualGrain.value === 'twoHours'
    ? `${point.label} 至 ${new Date(Date.parse(point.bucket) + 2 * 3600000 + props.summary!.offsetMinutes * 60000).toISOString().slice(5, 16).replace('T', ' ')}`
    : point.label;
  const labels = { label: point.label, tooltipLabel };
  if (props.metric === 'cost') return { ...labels, value: Number(point.picoUsd), valueLabel: formatMoney(point.picoUsd, 'usd4') };
  if (props.metric === 'hitRate') return { ...labels, value: point.cacheHitRate };
  return { ...labels, value: point.inputTokens + point.outputTokens };
}) ?? []);
</script>

<template>
  <section class="surface trend-panel" aria-label="用量趋势">
    <header><h2>用量趋势</h2><div class="trend-tools">
      <SegmentedControl :model-value="metric" label="趋势指标" :options="[{ value: 'tokens', label: 'Token' }, { value: 'cost', label: '成本' }, { value: 'hitRate', label: '命中率' }]" @update:model-value="emit('update:metric', $event)" />
      <SegmentedControl :model-value="grain" label="趋势粒度" :options="dailyOnly ? [{ value: 'day', label: '天' }] : [{ value: 'hour', label: '小时' }, { value: 'day', label: '天' }]" @update:model-value="emit('update:grain', $event)" />
    </div></header>
    <p v-if="actualGrain !== grain" class="text-muted grain-note">实际粒度：{{ actualGrain === 'day' ? '天' : actualGrain === 'twoHours' ? '2小时' : actualGrain === 'hour' ? '小时' : '分钟' }}</p>
    <BarChart :points="points" :label="metric === 'tokens' ? 'Token 趋势' : metric === 'cost' ? '成本趋势' : '命中率趋势'" :y-format="metric === 'hitRate' ? 'percent' : metric" :x-tick-count="4" @select="actualGrain === 'day' && summary?.trend[$event] && emit('selectDay', summary.trend[$event]!.bucket)" />
  </section>
</template>

<style scoped>
.trend-panel { padding: 20px; }header, .trend-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }header { justify-content: space-between; }
.grain-note { margin-block: 10px 16px; font-size: 12px; }
</style>
