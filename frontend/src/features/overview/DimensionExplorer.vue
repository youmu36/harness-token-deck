<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { formatMoney } from '../../shared/money';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import type { UsageRange, UsageHarness } from '../usage-common/types';
import type { ExploreScope } from './scope';
import ThroughputReference from './ThroughputReference.vue';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import MultiLineChart, { type LineSeries } from '../../ui/charts/MultiLineChart.vue';

const props = defineProps<{ scope: ExploreScope; range: UsageRange; harness: UsageHarness; mode: TimeZoneMode; options?: CommandData<'stats.exploreOptions'>; kind?: 'statistics' | 'cache' | 'value' | 'quality'; initialModel?: string; resolveRange?: () => UsageRange }>();
const bridge = inject(BridgeKey)!;
const focus = ref(props.initialModel ?? '');
const offset = ref(0);
const qualityOffset = ref(0);
const metric = ref<'tokens' | 'cost' | 'hitRate' | 'value'>(props.kind === 'cache' ? 'hitRate' : props.kind === 'value' ? 'value' : 'tokens');
const grain = ref<'hour' | 'day'>('day');
const parameters = computed<RequestMap['stats.explore']>(() => ({
  ...props.scope, harnesses: props.scope.harnesses?.length ? props.scope.harnesses : props.harness === 'all' ? [] : [props.harness],
  start: props.range.start, end: props.range.end, modelKeys: focus.value ? [focus.value] : props.scope.modelKeys,
  grain: grain.value, offset: offset.value, limit: 50, qualityOffset: qualityOffset.value,
}));
const key = computed(() => JSON.stringify(parameters.value));
const query = useQuery('overview.dimensions', async () => { const bounds = props.resolveRange?.() ?? props.range; return unwrap(await bridge.call('stats.explore', {...parameters.value, start:bounds.start, end:bounds.end})); }, {
  deps: [key], clearOnChange: true, coalesce: true, eventIntervalMs: props.kind === 'statistics' ? 5000 : 86400000,
  events: [{ name: 'usage.changed', when: event => !parameters.value.harnesses?.length || parameters.value.harnesses.some(source => event.harnesses.includes(source)) }],
});
const snapshot = computed(() => query.data.value);
const integer = (value?: string | null) => value == null ? '—' : BigInt(value).toLocaleString('en-US');
const rate = (value?: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const decimal = (value?: string | null) => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 1 });
const lookup = (kind: 'vendors' | 'groups' | 'versions', id: string) => props.options?.[kind].find(row => row.id === id)?.name ?? (id ? '历史项目' : kind === 'vendors' ? '未关联' : '信息缺失');
type Breakdown = CommandData<'stats.explore'>['breakdowns'][number];
const rowKey = (row: Breakdown) => JSON.stringify([row.harness, row.vendorId, row.groupId, row.versionId, row.modelKey]);
const rowName = (row: Breakdown) => `${lookup('vendors', row.vendorId)} / ${lookup('groups', row.groupId)} / ${lookup('versions', row.versionId)} / ${row.displayName}`;
const selected = ref<string[]>([]);
watch([() => props.scope, () => props.range, focus], () => { offset.value = 0; qualityOffset.value = 0; selected.value = []; }, { deep: true });
const compared = computed(() => snapshot.value?.breakdowns.filter(row => selected.value.includes(rowKey(row))) ?? []);
function compare(row: Breakdown, checked: boolean) { selected.value = checked ? [...selected.value, rowKey(row)].slice(0, 5) : selected.value.filter(key => key !== rowKey(row)); }
const compareKey = computed(() => JSON.stringify([parameters.value, compared.value.map(rowKey)]));
const comparisons = useQuery('overview.dimensionComparison', async () => {
  const rows = compared.value;
  const data = await Promise.all(rows.map(row => bridge.call('stats.explore', { ...parameters.value, start:snapshot.value?.start ?? parameters.value.start, end:snapshot.value?.end ?? parameters.value.end, offset: 0, harnesses: [row.harness as NonNullable<RequestMap['stats.explore']['harnesses']>[number]], vendorIds: [row.vendorId], groupIds: row.groupId ? [row.groupId] : [], versionIds: row.versionId ? [row.versionId] : [], modelKeys: [row.modelKey] }).then(unwrap)));
  return rows.map((row, index) => ({ row, data: data[index]! }));
}, { deps: [compareKey], visible: computed(() => compared.value.length > 0), clearOnChange: true });
const series = computed<LineSeries[]>(() => (comparisons.data.value ?? []).map(({ row, data }) => ({ key: rowKey(row), name: rowName(row), points: data.trend.map(point => ({ day: point.label, value: metric.value === 'hitRate' ? point.cacheHitRate === null ? null : point.cacheHitRate * 100 : metric.value === 'value' ? point.tokensPerDollar === null ? null : Number(point.tokensPerDollar) : metric.value === 'cost' ? Number(point.picoUsd) / 1e12 : Number(point.totalTokens), label: pointLabel(point) })) })));
function pointLabel(point: CommandData<'stats.explore'>['totals']) { return metric.value === 'hitRate' ? rate(point.cacheHitRate) : metric.value === 'value' ? `${decimal(point.tokensPerDollar)} Token / $1` : metric.value === 'cost' ? formatMoney(point.picoUsd, 'usd4') : integer(point.totalTokens); }
const points = computed(() => snapshot.value?.trend.map(point => ({ label: point.label, value: metric.value === 'hitRate' ? point.cacheHitRate : metric.value === 'value' ? point.tokensPerDollar === null ? null : Number(point.tokensPerDollar) : Number(metric.value === 'cost' ? point.picoUsd : point.totalTokens), valueLabel: pointLabel(point) })) ?? []);
const pendingAck = ref(false);
const ackError = ref('');
async function acknowledge(items = snapshot.value?.quality.filter(item => !item.acknowledged) ?? []) {
  pendingAck.value = true; ackError.value = '';
  try { unwrap(await bridge.call('stats.acknowledgeQuality', { items: items.map(({ id, fingerprint }) => ({ id, fingerprint })) })); await query.refresh(); }
  catch (error) { ackError.value = error instanceof Error ? error.message : '确认缺失失败，请重试'; }
  finally { pendingAck.value = false; }
}
const showAcknowledged = ref(true);
const issues = computed(() => snapshot.value?.quality.filter(row => showAcknowledged.value || !row.acknowledged) ?? []);
</script>

<template>
  <section class="dimension-explorer" :aria-busy="query.loading.value || query.refreshing.value">
    <header><div><Button v-if="focus && !initialModel" @click="focus = ''">返回模型排名</Button><h2>{{ kind === 'quality' ? '缺失记录' : focus ? `${snapshot?.models[0]?.displayName ?? '模型'} · 模型详情` : kind === 'cache' ? '供应商与模型缓存表现' : kind === 'value' ? '模型性价比' : '筛选结果' }}</h2></div><Button :pending="query.refreshing.value" @click="query.refresh">更新{{ kind === 'statistics' ? '统计' : '分析' }}</Button></header>
    <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
    <p v-if="query.loading.value" class="text-muted">正在按所选条件加载…</p>
    <template v-if="snapshot">
      <p class="as-of text-muted">{{ formatDateTime(snapshot.generatedAt, mode) }} 更新 · {{ snapshot.businessTimezone }} · 当天数据尚未完整{{ kind === 'cache' || kind === 'value' ? '；被动分析按天更新' : '' }}</p>
      <template v-if="kind === 'quality'">
        <p class="text-muted">确认缺失只关闭提醒，保留原始统计及缺失原因。已确认记录仍可查询；新增缺失会重新提醒。</p>
        <div class="quality-actions"><label><input v-model="showAcknowledged" type="checkbox" />显示已确认记录</label><Button :pending="pendingAck" :disabled="!snapshot.quality.some(row => !row.acknowledged)" @click="acknowledge()">确定已知有缺失，关闭本页提醒</Button><span>{{ snapshot.qualityTotal }} 组 · {{ snapshot.qualityUnacknowledged }} 组未确认</span></div>
        <InlineAlert v-if="ackError" severity="danger">{{ ackError }}</InlineAlert>
        <div class="table-wrap"><table aria-label="缺失记录列表"><thead><tr><th>日期</th><th>来源 / 模型</th><th>供应商</th><th>缺失信息</th><th>记录数</th><th>状态</th></tr></thead><tbody><tr v-for="item in issues" :key="item.id"><td>{{ item.date }}</td><td>{{ item.harness }} / {{ item.displayName }}</td><td>{{ lookup('vendors', item.vendorId) }}</td><td>{{ item.reasons.join('、') }}<span v-if="item.costDifferencePico"> · 差额 <Money :pico="item.costDifferencePico" /></span></td><td>{{ integer(item.recordCount) }}</td><td><span v-if="item.acknowledged">已确认</span><Button v-else :pending="pendingAck" @click="acknowledge([item])">已知缺失，关闭提醒</Button></td></tr><tr v-if="!issues.length"><td colspan="6">当前条件没有{{ showAcknowledged ? '' : '未确认的' }}缺失记录</td></tr></tbody></table></div><footer><Button v-if="qualityOffset" @click="qualityOffset = Math.max(0, qualityOffset - 100)">上一页</Button><Button v-if="snapshot.qualityNextOffset !== null" @click="qualityOffset = snapshot.qualityNextOffset">下一页</Button></footer>
      </template>
      <template v-else>
        <InlineAlert v-if="snapshot.qualityUnacknowledged"><div class="quality-actions"><span>{{ snapshot.qualityUnacknowledged }} 组数据有缺失，可在“缺失记录”页查询日期与原因。</span><Button :pending="pendingAck" @click="acknowledge()">确定已知有缺失，关闭本页提醒</Button></div></InlineAlert>
        <InlineAlert v-if="ackError" severity="danger">{{ ackError }}</InlineAlert>
        <p v-for="notice in snapshot.notices.filter(note => !note.includes('缺少') && !note.includes('缺失'))" :key="notice" class="capability text-muted">{{ notice }}</p>
        <div class="explore-metrics"><article><span>总 Token</span><strong>{{ integer(snapshot.totals.totalTokens) }}</strong><small>输入 {{ integer(snapshot.totals.inputTokens) }} / 输出 {{ integer(snapshot.totals.outputTokens) }}</small></article><article><span>本地估算成本</span><strong><Money :pico="snapshot.totals.picoUsd" /></strong><small>{{ integer(snapshot.totals.recordCount) }} 条请求</small></article><article><span>缓存命中率</span><strong>{{ rate(snapshot.totals.cacheHitRate) }}</strong><small>按已知输入加权</small></article><article v-if="kind === 'value' || focus"><span>每 $1 可用 Token</span><strong>{{ decimal(snapshot.totals.tokensPerDollar) }}</strong><small>仅计入完整、已定价样本</small></article></div>
        <ThroughputReference v-if="focus" :scope="{...scope, modelKeys:[focus]}" :options="options" />
        <section v-if="kind === 'value' || focus" class="value-parts surface"><h3>每美元用量结构</h3><div><article v-for="(label, part) in { input: '未命中输入', cache: '缓存命中', output: '输出', write: '缓存创建' }" v-show="part !== 'write' || snapshot.totals.parts.write !== '0'" :key="part"><span>{{ label }}</span><strong>{{ decimal(snapshot.totals.perDollar[part]) }}</strong><small>{{ BigInt(snapshot.totals.eligibleTokens) ? `${(Number(snapshot.totals.parts[part]) / Number(snapshot.totals.eligibleTokens) * 100).toFixed(1)}%` : '—' }}</small></article></div><p class="text-muted">按历史使用结构与本地估算成本计算，{{ formatMoney(snapshot.totals.eligibleCostPico, 'usd4') }} 纳入计算；不代表模型质量。缓存命中已含在输入中；仅有实际创建费用时单列缓存创建。</p></section>
        <section class="surface explore-chart"><header><h3>{{ focus ? '模型历史趋势' : '趋势与比较' }}</h3><div class="chart-controls"><ChoiceMenu v-model="metric" label="趋势指标" :options="[{value:'tokens',label:'Token'}, {value:'cost',label:'本地成本'}, {value:'hitRate',label:'缓存命中率'}, {value:'value',label:'Token / 美元'}]" /><ChoiceMenu v-if="kind === 'statistics'" v-model="grain" label="时间粒度" :options="[{value:'day',label:'按天'}, {value:'hour',label:'小时明细'}]" /></div></header><InlineAlert v-if="comparisons.error.value" severity="danger" retry @retry="comparisons.refresh">{{ comparisons.error.value.message }}</InlineAlert><MultiLineChart v-if="series.length" :series="series" label="供应商和倍率版本比较趋势" :unit="metric === 'hitRate' ? '%' : metric === 'cost' ? ' USD' : ''" /><BarChart v-else :points="points" :y-format="metric === 'hitRate' ? 'percent' : metric === 'cost' ? 'cost' : 'tokens'" :variant="metric === 'hitRate' ? 'line' : 'bar'" label="筛选统计趋势" :x-tick-count="6" /></section>
        <section class="surface explore-table"><header><h3>模型{{ focus ? '用量' : '排名' }} · 本地估算成本</h3><span>{{ snapshot.totalModels }} 个模型</span></header><div class="table-wrap"><table aria-label="筛选模型排名"><thead><tr><th>模型</th><th>Token</th><th>本地估算</th><th>缓存命中率</th><th>Token / $1</th><th></th></tr></thead><tbody><tr v-for="model in snapshot.models" :key="model.modelKey"><td>{{ model.displayName }}</td><td>{{ integer(model.totalTokens) }}</td><td><Money :pico="model.picoUsd" /></td><td>{{ rate(model.cacheHitRate) }}</td><td>{{ decimal(model.tokensPerDollar) }}</td><td><Button v-if="!focus" @click="focus = model.modelKey">模型详情</Button></td></tr><tr v-if="!snapshot.models.length"><td colspan="6">当前筛选暂无记录</td></tr></tbody></table></div><footer><Button v-if="offset" @click="offset = Math.max(0, offset - 50)">上一页</Button><Button v-if="snapshot.nextOffset !== null" @click="offset = snapshot.nextOffset">下一页</Button></footer></section>
        <section class="surface explore-table"><header><h3>供应商 / 价格组 / 倍率版本</h3><span>勾选最多五项比较{{ snapshot.breakdowns.length === 100 ? ' · 当前仅列前100项，可缩小筛选' : '' }}</span></header><div class="table-wrap"><table aria-label="供应商版本比较"><thead><tr><th>比较</th><th>供应商</th><th>价格组</th><th>倍率版本</th><th>来源 / 模型</th><th>Token</th><th>命中率</th><th>Token / $1</th></tr></thead><tbody><tr v-for="row in snapshot.breakdowns" :key="rowKey(row)"><td><input type="checkbox" :aria-label="`比较 ${rowName(row)}`" :checked="selected.includes(rowKey(row))" :disabled="(!row.versionId || selected.length >= 5) && !selected.includes(rowKey(row))" @change="compare(row, ($event.target as HTMLInputElement).checked)" /></td><td>{{ lookup('vendors', row.vendorId) }}</td><td>{{ lookup('groups', row.groupId) }}</td><td>{{ lookup('versions', row.versionId) }}</td><td>{{ row.harness }} / {{ row.displayName }}</td><td>{{ integer(row.totalTokens) }}</td><td>{{ rate(row.cacheHitRate) }}</td><td>{{ decimal(row.tokensPerDollar) }}</td></tr></tbody></table></div></section>
      </template>
    </template>
  </section>
</template>

<style scoped>
.dimension-explorer{display:grid;gap:18px;min-width:0}header,footer,.chart-controls,.quality-actions{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}header h2{font-size:19px}header h3{font-size:15px}header>span,.as-of,.capability,.quality-actions,p{font-size:12px;line-height:1.7}.explore-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:20px;border-block:1px solid var(--border);padding:22px 0}.explore-metrics article,.value-parts article{display:grid;gap:10px}.explore-metrics strong{font-size:24px;font-weight:600;overflow-wrap:anywhere}.explore-metrics span,.explore-metrics small,.value-parts small{font-size:12px;color:var(--text-muted)}.explore-chart,.explore-table,.value-parts{padding:20px;display:grid;gap:16px;min-width:0}.value-parts>div{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px}.value-parts strong{font-size:21px;color:var(--money)}.value-parts span{font-size:13px}.table-wrap{overflow:auto;max-height:470px;border:1px solid var(--border);border-radius:5px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:11px 12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border)}th{position:sticky;top:0;background:var(--surface-2);z-index:1}footer{justify-content:flex-end}.quality-actions label{display:flex;align-items:center;gap:8px}input[type=checkbox]{min-height:0}
</style>
