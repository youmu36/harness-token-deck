<script setup lang="ts">
import { computed } from 'vue';
import MetricCard from '../../ui/MetricCard.vue';
import Money from '../../ui/Money.vue';
import { formatInteger, formatPercent, formatTokenCount, formatMoney } from '../../shared/format';
import { metricNotes } from './metricNotes';
import type { Metrics, UsageHarness } from './types';
const props = defineProps<{ metrics?: Metrics; harness: UsageHarness; loading?: boolean; auxiliaryPico?: string }>();
const cost = computed(() => props.metrics ? (BigInt(props.metrics.picoUsd)+BigInt(props.auxiliaryPico ?? '0')).toString() : undefined);
const notes = computed(() => props.metrics ? metricNotes(props.metrics, props.harness) : undefined);
const cards = computed(() => [
  { title: '新增输入', value: props.metrics?.cacheCoverageIncomplete ? null : props.metrics?.newInput, note: notes.value?.newInput },
  { title: '缓存命中', value: props.metrics?.cacheReadTokens, note: notes.value?.cacheRead },
  { title: '输出', value: props.metrics?.outputTokens, note: notes.value?.output },
]);
</script>

<template>
  <section class="metrics-grid" aria-label="用量指标">
    <MetricCard v-for="card in cards" :key="card.title" :title="card.title" :value="formatTokenCount(card.value).split(' ')[0]" :unit="formatTokenCount(card.value).split(' ')[1]" :full-value="formatInteger(card.value)" :description="card.note" :loading="loading" />
    <MetricCard title="缓存命中率" :value="formatPercent(metrics?.cacheHitRate)" :description="notes?.hitRate" :loading="loading" />
    <MetricCard title="总 token" :value="formatTokenCount(metrics?.totalTokens).split(' ')[0]" :unit="formatTokenCount(metrics?.totalTokens).split(' ')[1]" :full-value="formatInteger(metrics?.totalTokens)" :description="notes?.totalTokens" :loading="loading" />
    <MetricCard title="估算成本 · USD" money :full-value="metrics ? formatMoney(cost!, 'full6') : undefined" :description="auxiliaryPico && BigInt(auxiliaryPico) > 0n ? '含生成会话标题的辅助成本，用量未知' : notes?.cost" :loading="loading">
      <template #value><Money v-if="metrics" :pico="cost!" /><span v-else>--</span></template>
    </MetricCard>
  </section>
</template>

<style scoped>
.metrics-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 1200px) { .metrics-grid { gap: 10px; } }
@media (max-width: 900px) { .metrics-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 560px) { .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
