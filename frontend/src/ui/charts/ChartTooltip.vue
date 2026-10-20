<script setup lang="ts">
import type { AxisFormat, ChartPoint } from './axis';
import { formatValue } from './axis';
defineProps<{ id: string; point: ChartPoint; yFormat: AxisFormat; left: number; warningGrain: 'minute' | 'hour' }>();
</script>

<template>
  <div :id="id" role="tooltip" class="chart-tooltip" :style="{ left: `${left}%` }">
    <strong>{{ point.tooltipLabel ?? point.label }}</strong>
    <p>{{ yFormat === 'percent' ? '缓存命中率' : '数值' }}：{{ point.value === null ? '--（未知）' : point.valueLabel ?? formatValue(point.value, yFormat) }}</p>
    <p v-if="point.flags?.cacheBust" class="bust">缓存击穿：上下文 ≥ 5 万且缓存命中率低于 10%</p>
    <p v-else-if="point.flags?.cacheLoss" class="loss">发生缓存大量未命中</p>
    <p v-if="point.flags?.contextCompacted" class="bust">上下文压缩：来源已明确标记</p>
    <p v-for="(segment, index) in point.segments" :key="index">
      {{ segment.label }}：{{ formatValue(segment.value, yFormat) }}
    </p>
  </div>
</template>

<style scoped>
.chart-tooltip { position: absolute; top: 8px; z-index: 2; pointer-events: none; width: 230px; max-width: 90%; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); color: var(--text); box-shadow: 0 3px 12px #0002; font-size: 12px; }
strong { display: block; margin-bottom: 4px; }
.bust { color: var(--cache-bust); }.loss { color: var(--cache-loss); }
</style>
