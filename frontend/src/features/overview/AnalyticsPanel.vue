<script setup lang="ts">
import { computed, inject, ref, shallowRef, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { formatMoney } from '../../shared/money';
import { formatDateTime, formatTokenCount } from '../../shared/format';
import type { UsageHarness, UsageRange, UsageFilter } from '../usage-common/types';
import { calculateRange } from '../usage-common/useTimeRange';
import type { TimeZoneMode } from '../../shared/time';
import type { ChartPoint } from '../../ui/charts/axis';
import Button from '../../ui/Button.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Money from '../../ui/Money.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import ThroughputReference from './ThroughputReference.vue';

type Period = RequestMap['stats.analytics']['period'];
type Model = CommandData<'stats.analytics'>['models'][number];
const props = defineProps<{ period: Period; models: boolean; statistics?: boolean; harness: UsageHarness; range: UsageRange; filter: UsageFilter; mode: TimeZoneMode }>();
const emit = defineEmits<{ 'update:harness': [value: UsageHarness] }>();
const bridge = inject(BridgeKey)!;
const year = ref(new Date().getFullYear());
const endYear = ref(new Date().getFullYear());
const quarter = ref(Math.floor(new Date().getMonth() / 3) + 1);
const offset = ref(0);
const focus = ref<string | null>(null);
const metric = ref<'tokens' | 'cost' | 'hitRate'>('tokens');
const compared = shallowRef<Model[]>([]);
const parameters = computed<RequestMap['stats.analytics']>(() => ({
  harness: props.harness, period: props.period,
  year: year.value, endYear: endYear.value, quarter: quarter.value,
  start: props.period === 'range' ? props.range.start : null,
  end: props.period === 'range' ? props.range.end : null,
  modelKey: focus.value, offset: offset.value, limit: 50,
}));
const key = computed(() => JSON.stringify(parameters.value));
const query = useQuery('overview.analytics', async () => {
  const range = props.period === 'range' ? calculateRange(props.filter, props.mode) : undefined;
  return unwrap(await bridge.call('stats.analytics', { ...parameters.value, ...(range ? { start: range.start, end: range.end } : {}) }));
}, {
  deps: [key], clearOnChange: true, coalesce: true, eventIntervalMs: 60_000,
  events: [{ name: 'usage.changed', when: event => props.harness === 'all' || event.harnesses.includes(props.harness) }, { name: 'output.changed', when: () => !props.statistics && props.models }],
});
watch([() => props.harness, () => props.period, year, endYear, quarter, focus], () => { offset.value = 0; compared.value = []; }, { flush: 'sync' });
watch(() => props.models, () => { focus.value = null; compared.value = []; });
let comparedRevision: number | undefined;
watch(query.data, data => { if (data && data.revision !== comparedRevision) { compared.value = []; comparedRevision = data.revision; } });
const snapshot = computed(() => query.data.value);
const totals = computed(() => snapshot.value?.totals);
const sourceNames: Record<string, string> = { codex: 'Codex', claude: 'Claude Code', dsh: 'DSH', zcode: 'Zcode' };
const integer = (value: string | undefined) => value === undefined ? '—' : BigInt(value).toLocaleString('en-US');
const hitRate = (value: number | null | undefined) => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const percent = (value: string | null | undefined) => value == null ? '—（上期为零）' : `${Number(value) > 0 ? '+' : ''}${value}%`;
const points = computed<ChartPoint[]>(() => snapshot.value?.trend.map(point => ({
  label: point.label,
  value: metric.value === 'hitRate' ? point.cacheHitRate : Number(metric.value === 'cost' ? point.picoUsd : point.totalTokens),
  valueLabel: metric.value === 'hitRate' ? hitRate(point.cacheHitRate) : metric.value === 'cost' ? formatMoney(point.picoUsd, 'usd4') : integer(point.totalTokens),
})) ?? []);
function compare(model: Model) {
  if (compared.value.some(row => row.modelKey === model.modelKey)) compared.value = compared.value.filter(row => row.modelKey !== model.modelKey);
  else if (compared.value.length < 3) compared.value = [...compared.value, model];
}
</script>

<template>
  <div class="analysis-panel" :aria-busy="query.loading.value">
    <div class="analysis-filters">
      <ChoiceMenu :model-value="harness" label="数据来源" :options="[{value:'all',label:'全部'}, {value:'claude',label:'Claude Code'}, {value:'codex',label:'Codex'}, {value:'zcode',label:'Zcode'}, {value:'dsh',label:'DSH'}]" @update:model-value="emit('update:harness', $event as UsageHarness)" />
      <label v-if="period === 'year' || period === 'years' || period === 'quarter'">{{ period === 'years' ? '起始年份' : '年份' }}<input v-model.number.lazy="year" type="number" min="1971" :max="new Date().getFullYear()" aria-label="分析年份" /></label>
      <label v-if="period === 'years'">结束年份<input v-model.number.lazy="endYear" type="number" :min="year" :max="new Date().getFullYear()" aria-label="结束年份" /></label>
      <label v-if="period === 'quarter'">季度<select v-model.number="quarter" aria-label="分析季度"><option v-for="q in 4" :key="q" :value="q">第 {{ q }} 季度</option></select></label>
      <Button :pending="query.loading.value || query.refreshing.value" @click="query.refresh">{{ statistics ? '更新统计' : '更新分析' }}</Button>
      <Button v-if="focus !== null" @click="focus = null">返回模型排名</Button>
    </div>
    <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
    <p v-if="query.loading.value" class="text-muted">正在读取分析…</p>
    <template v-if="snapshot && totals">
      <div class="analysis-caption text-muted"><span>{{ formatDateTime(snapshot.start, mode) }} — {{ formatDateTime(snapshot.end, mode) }} · {{ snapshot.businessTimezone }}</span><span>上次更新：{{ formatDateTime(snapshot.generatedAt, mode) }}</span></div>
      <InlineAlert v-if="snapshot.includesWholeBoundaryDays || snapshot.comparison?.includesWholeBoundaryDays">已精简历史包含完整边界业务日，边界日无法再按小时切分；周期比较按此覆盖范围计算。</InlineAlert>
      <div v-if="statistics" class="analysis-metrics">
        <article class="surface"><h2>总 Token</h2><strong>{{ integer(totals.totalTokens) }}</strong><span v-if="BigInt(totals.totalTokens) >= 10000n" aria-label="Token 大单位参考">约 {{ formatTokenCount(BigInt(totals.totalTokens)) }}</span><span v-if="snapshot.comparison">较上期 {{ percent(snapshot.comparison.changes.tokens) }}</span></article>
        <article class="surface"><h2>本地估算成本</h2><strong><Money :pico="totals.picoUsd" /></strong><span v-if="snapshot.comparison">较上期 {{ percent(snapshot.comparison.changes.cost) }}</span></article>
        <article class="surface"><h2>缓存命中率</h2><strong>{{ hitRate(totals.cacheHitRate) }}</strong><span v-if="totals.cacheCoverageIncomplete">仅按缓存已知输入加权</span></article>
        <article class="surface"><h2>请求数</h2><strong>{{ integer(totals.recordCount) }}</strong><span>{{ totals.coveredDays }} 个有记录业务日</span></article>
      </div>
      <p v-if="totals.incomplete || totals.unpriced || totals.unknownModelCount !== '0'" class="quality text-muted">{{ totals.unpriced ? '含未定价用量。' : '' }}{{ totals.incomplete ? '部分成本或用量不完整。' : '' }}{{ totals.unknownModelCount !== '0' ? `${integer(totals.unknownModelCount)} 条模型未知。` : '' }}</p>
      <section v-if="statistics" class="surface analysis-trend" aria-label="长期趋势">
        <header><h2>{{ focus === null ? '用量趋势' : `${snapshot.models[0]?.displayName ?? '模型'} · 历史` }}</h2><ChoiceMenu v-model="metric" label="长期趋势指标" :options="[{value:'tokens',label:'Token'}, {value:'cost',label:'本地成本'}, {value:'hitRate',label:'命中率'}]" /></header>
        <BarChart :points="points" :label="`${snapshot.grain === 'year' ? '年度' : snapshot.grain === 'month' ? '月度' : '每日'}趋势`" :y-format="metric === 'hitRate' ? 'percent' : metric" :x-tick-count="6" />
      </section>
      <section v-if="!models" class="surface analysis-table" aria-label="长期来源结构"><h2>来源结构</h2><div class="table-wrap"><table><thead><tr><th>来源</th><th>请求</th><th>Token</th><th>本地估算</th><th>命中率</th><th>覆盖</th></tr></thead><tbody><tr v-for="source in snapshot.harnesses" :key="source.harness"><td>{{ sourceNames[source.harness] }}</td><td>{{ integer(source.recordCount) }}</td><td>{{ integer(source.totalTokens) }}</td><td><Money :pico="source.picoUsd" /></td><td>{{ hitRate(source.cacheHitRate) }}</td><td>{{ source.coverageStart?.slice(0,10) }} — {{ source.coverageEnd?.slice(0,10) }}</td></tr><tr v-if="!snapshot.harnesses.length"><td colspan="6">当前范围暂无用量</td></tr></tbody></table></div></section>
      <template v-else>
        <ThroughputReference :snapshot="snapshot" />
        <section class="surface analysis-table" aria-label="模型长期排名"><header><h2>模型排名 · 本地估算成本</h2><span class="text-muted">{{ snapshot.totalModels }} 个模型 · 第 {{ Math.floor(offset / 50) + 1 }} 页</span></header><div class="table-wrap"><table><thead><tr><th>比较</th><th>模型</th><th>本地估算</th><th>Token</th><th>命中率</th><th>活跃分钟</th><th>token/秒</th><th>历史</th></tr></thead><tbody><tr v-for="model in snapshot.models" :key="model.modelKey"><td><input type="checkbox" :aria-label="`比较 ${model.displayName}`" :checked="compared.some(row => row.modelKey === model.modelKey)" :disabled="compared.length >= 3 && !compared.some(row => row.modelKey === model.modelKey)" @change="compare(model)" /></td><td>{{ model.displayName }}</td><td><Money :pico="model.picoUsd" /></td><td>{{ integer(model.totalTokens) }}</td><td>{{ hitRate(model.cacheHitRate) }}</td><td>{{ integer(model.activeMinutes) }}</td><td>{{ model.tokensPerSecond ?? '—' }}</td><td><Button @click="focus = model.modelKey">查看历史</Button></td></tr><tr v-if="!snapshot.models.length"><td colspan="8">当前范围暂无模型用量</td></tr></tbody></table></div><footer><Button v-if="offset > 0" @click="offset = Math.max(0, offset - 50)">上一页</Button><Button v-if="snapshot.nextOffset !== null" @click="offset = snapshot.nextOffset">下一页</Button></footer></section>
        <section v-if="compared.length" class="surface analysis-table" aria-label="模型比较"><h2>模型比较</h2><div class="table-wrap"><table><thead><tr><th>模型</th><th>请求数</th><th>已知输出</th><th>本地估算</th><th>命中率</th><th>token/秒</th><th>样本</th></tr></thead><tbody><tr v-for="model in compared" :key="model.modelKey"><td>{{ model.displayName }}</td><td>{{ integer(model.recordCount) }}</td><td>{{ integer(model.outputTokens) }}</td><td><Money :pico="model.picoUsd" /></td><td>{{ hitRate(model.cacheHitRate) }}</td><td>{{ model.tokensPerSecond ?? '—' }}</td><td>{{ integer(model.activeMinutes) }} 分钟{{ model.missingCapabilities.includes('complete_output_samples') ? '（输出不完整）' : '' }}</td></tr></tbody></table></div></section>
      </template>
    </template>
  </div>
</template>

<style scoped>
.analysis-panel{display:grid;gap:24px;min-width:0}.analysis-filters,.analysis-caption,header,footer{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}.analysis-filters{justify-content:flex-start}.analysis-filters label{display:flex;align-items:center;gap:8px;font-size:13px}.analysis-filters input{width:100px}.analysis-caption,.quality{font-size:12px}.analysis-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.analysis-metrics article{padding:18px;display:grid;gap:12px}.analysis-metrics h2{font-size:13px;color:var(--text-muted)}.analysis-metrics strong{font-size:23px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.analysis-metrics strong :deep(.money){font-size:inherit;font-weight:inherit}.analysis-metrics span{font-size:12px;color:var(--text-muted)}.comparison,.analysis-trend,.analysis-table,.throughput{padding:20px;display:grid;gap:16px;min-width:0}.comparison p,.throughput p{font-size:13px}.throughput strong{color:var(--money)}.throughput strong span{color:var(--text-muted);font-weight:400}.table-wrap{overflow:auto;max-height:480px;border:1px solid var(--border);border-radius:6px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border)}th{position:sticky;top:0;background:var(--surface-2)}header>span{font-size:12px}footer{justify-content:flex-end}@media(max-width:900px){.analysis-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
