<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { CommandData } from '../../app/ports';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import type { ChartPoint } from '../../ui/charts/axis';
const props = defineProps<{ snapshot: CommandData<'stats.analytics'> }>();
const model = ref('');
watch(() => props.snapshot.models, models => { if (!models.some(row => row.modelKey === model.value)) model.value = models[0]?.modelKey ?? ''; }, { immediate: true });
const selected = computed(() => props.snapshot.models.find(row => row.modelKey === model.value));
const records = computed(() => (props.snapshot.outputReferences ?? []).filter(row => row.modelKey === model.value));
const speed = computed(() => selected.value?.tokensPerSecond == null ? '—' : Number(selected.value.tokensPerSecond).toFixed(1));
const points = computed<ChartPoint[]>(() => records.value.map(row => ({label:row.day,value:row.tokensPerSecond === null ? null : Number(row.tokensPerSecond),valueLabel:`${row.tokensPerSecond ?? '—'} t/s · ${row.samples} 次静默结算样本`})));
const units = (value: number) => `${value.toFixed(1)} t/s`;
</script>

<template>
  <section class="surface throughput-reference" aria-label="输出吞吐参考">
    <header><h2>输出吞吐参考 · 按模型</h2><ChoiceMenu v-if="snapshot.models.length" v-model="model" label="吞吐参考模型" :options="snapshot.models.map(row => ({value:row.modelKey,label:row.displayName}))" /></header>
    <strong>{{ selected?.displayName ?? '模型' }} tps：{{ speed }} t/s</strong>
    <p class="text-muted">长期 TPS 按已结每日 TPS 取平均；计时缺失时使用分钟估算，不代表真实生成速度。</p>
    <h3>每日输出吞吐参考 · 最近 30 天</h3>
    <BarChart :points="points" variant="line" :y-format="units" label="每日模型输出吞吐" :x-tick-count="6" empty-message="尚无已完成的吞吐参考样本；本地静默 10 分钟后保存。" />
    <p class="text-muted">最近 30 个业务日固定展示，不随分析周期改变。次日仅保留每日 TPS；用于计算的分钟记录与生成计时随后清理，重启前的待办独立续办。</p>
  </section>
</template>

<style scoped>
.throughput-reference{padding:20px;display:grid;gap:16px;min-width:0}header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}strong{font-size:20px;color:var(--money);font-variant-numeric:tabular-nums}p{font-size:13px;line-height:1.7}h3{font-size:14px;font-weight:500}
</style>
