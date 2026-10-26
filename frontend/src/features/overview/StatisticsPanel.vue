<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { BridgeKey, type RequestMap } from '../../app/ports';
import { readAnalysis } from './analysisQuery';
import { useQuery } from '../../shared/query';
import type { UsageRange } from '../usage-common/types';
import type { ExploreScope } from './scope';
import type { ChartPoint } from '../../ui/charts/axis';
import MetricCard from '../../ui/MetricCard.vue';
import Money from '../../ui/Money.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Button from '../../ui/Button.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import ExactUsageTable from './ExactUsageTable.vue';
import { formatTokenCount } from '../../shared/format';
import { formatMoney } from '../../shared/money';
const props = defineProps<{scope: ExploreScope; range: UsageRange; rangeKind: RequestMap['stats.explore']['rangeKind']; resolveRange: () => UsageRange; refreshToken:number}>();
const bridge = inject(BridgeKey)!, offset = ref(0), hours = ref(false);
const parameters = computed<RequestMap['stats.explore']>(() => ({...props.scope,start:props.range.start,end:props.range.end,rangeKind:props.rangeKind,grain:'day',sections:['totals','models','trend'],offset:offset.value,limit:50}));
watch(() => JSON.stringify([props.scope,props.range]), () => { offset.value = 0; hours.value = false; });
const query = useQuery('statistics.page', signal => { const bounds = props.resolveRange(); return readAnalysis(bridge,{...parameters.value,start:bounds.start,end:bounds.end},signal); }, {deps:[() => JSON.stringify(parameters.value),() => props.refreshToken],clearOnChange:true,coalesce:true,eventIntervalMs:5000,events:['cost.changed',{name:'usage.changed',when:event => !props.scope.harnesses?.length || props.scope.harnesses.some(h => event.harnesses.includes(h))}]});
const data = computed(() => query.data.value);
const hourQuery = useQuery('statistics.hours', signal => readAnalysis(bridge,{...parameters.value,grain:'hour',sections:['trend'],start:props.range.start,end:props.range.end},signal), {eventIntervalMs:60000,visible:hours,deps:[() => JSON.stringify(parameters.value)],clearOnChange:true,clearOnHide:true,coalesce:true,events:['usage.changed','cost.changed']});
const points = computed<ChartPoint[]>(() => data.value?.trend.map(row => ({label:row.label,value:Number(row.totalTokens),valueLabel:BigInt(row.totalTokens).toLocaleString('en-US'),segments:[{label:'新增输入',value:row.cacheCoverageIncomplete ? null : Number(row.newInput),color:'var(--usage-input)',glow:true},{label:'缓存命中',value:Number(row.cacheReadTokens),color:'var(--usage-cache)'},{label:'输出',value:Number(row.outputTokens),color:'var(--usage-output)',glow:true}]})) ?? []);
const cards = computed(() => [ {title:'新增输入',value:data.value?.totals.cacheCoverageIncomplete ? undefined : data.value?.totals.newInput}, {title:'缓存命中',value:data.value?.totals.cacheReadTokens}, {title:'输出',value:data.value?.totals.outputTokens} ]);
const count = (value?:string) => value == null ? '—' : BigInt(value).toLocaleString('en-US');
const models = computed(() => data.value?.models.map(row => ({...row,id:row.modelKey,name:row.displayName})) ?? []);
const sources = computed(() => data.value?.harnesses.map(row => ({...row,id:row.harness,name:row.displayName})) ?? []);
</script>
<template><section class="statistics-panel" aria-label="数据统计">
  <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
  <section class="metrics-grid" aria-label="用量指标"><MetricCard v-for="card in cards" :key="card.title" :title="card.title" :value="count(card.value)" :full-value="card.value" :description="card.value && BigInt(card.value) >= 10000n ? `约 ${formatTokenCount(BigInt(card.value))}` : undefined" :loading="query.loading.value" />
    <MetricCard title="缓存命中率" :value="data?.totals.cacheHitRate == null ? '—' : `${(data.totals.cacheHitRate*100).toFixed(1)}%`" :loading="query.loading.value" />
    <MetricCard title="总 Token" :value="count(data?.totals.totalTokens)" :description="data && BigInt(data.totals.totalTokens) >= 10000n ? `约 ${formatTokenCount(BigInt(data.totals.totalTokens))}` : undefined" :loading="query.loading.value" />
    <MetricCard title="本地有效成本 · USD" :description="data ? `含校准金额 ${formatMoney(data.totals.correctionPico, 'usd4')} USD` : undefined" money :loading="query.loading.value"><template #value><Money v-if="data" :pico="data.totals.picoUsd" /></template></MetricCard>
  </section>
  <p v-if="data" class="cost-basis">原估算 <Money :pico="data.totals.estimatePico ?? data.totals.picoUsd" /> + 校准差额 <Money :pico="data.totals.correctionPico" />；未分配 <Money :pico="data.totals.unallocatedCorrectionPico" />。{{ data.totals.allocationIncomplete ? '部分校准无法归属到当前模型或范围。' : '费用沿用当前范围的本地凭证。' }}</p>
  <InlineAlert v-for="item in data?.coverage ?? []" :key="item.code">{{ item.message }}</InlineAlert>
  <section class="surface usage-chart"><h2>用量历史</h2><p>新增输入、缓存命中、输出</p><BarChart :points="points" label="用量历史柱状图" y-format="tokens" :x-tick-count="6" /></section>
  <section aria-label="模型明细"><h2>模型明细</h2><ExactUsageTable :rows="models" label="模型明细表" /><footer><Button v-if="offset" @click="offset = Math.max(0,offset-50)">上一页模型</Button><Button v-if="data?.nextOffset != null" @click="offset = data!.nextOffset!">下一页模型</Button></footer></section>
  <details v-if="!scope.harnesses?.length || scope.harnesses.length > 1" class="source-summary surface"><summary>按来源汇总</summary><ExactUsageTable :rows="sources" label="按来源汇总表" /></details>
  <section><Button :aria-expanded="hours" @click="hours = !hours">{{ hours ? '收起小时明细' : '查看小时明细' }}</Button><template v-if="hours"><InlineAlert v-if="hourQuery.error.value" severity="danger">{{ hourQuery.error.value.message }}</InlineAlert><p v-if="hourQuery.data.value?.grain !== 'hour'">当前范围仅有{{ hourQuery.data.value?.grain === 'month' ? '月' : '日' }}级能力，请缩小到近 7 天；不会把汇总伪造成小时。</p><ExactUsageTable v-else :rows="hourQuery.data.value?.trend.map(row => ({...row,id:row.bucket,name:row.label})) ?? []" label="小时明细表" /></template></section>
</section></template>
<style scoped>
.statistics-panel{display:grid;gap:22px;min-width:0}.metrics-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.usage-chart{padding:22px;min-width:0}.usage-chart p,.cost-basis{font-size:12px;line-height:1.8;color:var(--text-muted)}h2{font-size:17px;margin-bottom:12px}.source-summary{padding:18px}summary{cursor:pointer;font-size:14px;margin-bottom:12px}footer{display:flex;justify-content:flex-end;gap:12px;margin-top:12px}@media(max-width:1100px){.metrics-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:560px){.metrics-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
