<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { formatMoney } from '../../shared/money';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import type { UsageRange, UsageHarness } from '../usage-common/types';
import type { ExploreScope } from './scope';
import { defaultAnalysisView, type AnalysisView } from './analysisState';
import { readAnalysis } from './analysisQuery';
import ThroughputReference from './ThroughputReference.vue';
import UsageComposition from './UsageComposition.vue';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import MultiLineChart, { type LineSeries } from '../../ui/charts/MultiLineChart.vue';

const props = defineProps<{ scope: ExploreScope; range: UsageRange; harness: UsageHarness; mode: TimeZoneMode; options?: CommandData<'stats.exploreOptions'>; kind?: 'statistics' | 'cache' | 'value' | 'quality'; initialModel?: string; resolveRange?: () => UsageRange; costBasis?: 'local' | 'remote'; view?: AnalysisView; refreshToken?: number }>();
const bridge = inject(BridgeKey)!;
const localView = ref(defaultAnalysisView());
const state = computed(() => props.view ?? localView.value);
const focus = ref<string | null>(props.initialModel ?? state.value.focus);
watch(focus, value => { state.value.focus = value; });
const offset = ref(0), qualityOffset = ref(0), accountOffset = ref(0), correctionOffset = ref(0);
const root = ref<HTMLElement | null>(null);
const showAcknowledged = ref(true);
const remote = computed(() => props.costBasis === 'remote' && props.kind !== 'quality');
const metric = ref<'tokens' | 'cost' | 'hitRate' | 'value'>(props.kind === 'cache' ? 'hitRate' : props.kind === 'value' ? 'value' : 'tokens');
const grain = ref<'hour' | 'day'>('day');
watch(() => props.scope, () => { offset.value = 0; qualityOffset.value = 0; accountOffset.value = 0; correctionOffset.value = 0; if (props.initialModel === undefined) focus.value = null; }, { deep: true });
watch([() => props.range, focus, showAcknowledged, remote], () => { offset.value = 0; qualityOffset.value = 0; accountOffset.value = 0; correctionOffset.value = 0; });
const parameters = computed<RequestMap['stats.explore']>(() => ({
  ...props.scope, start: props.range.start, end: props.range.end,
  modelKeys: focus.value !== null ? [focus.value] : props.scope.modelKeys,
  grain: grain.value, offset: offset.value, limit: 50, qualityOffset: qualityOffset.value,
  qualityStatus: showAcknowledged.value ? 'all' : 'unacknowledged', accountOffset: accountOffset.value, accountCorrectionOffset: correctionOffset.value,
  sections: props.kind === 'quality' ? ['quality'] : ['totals', 'models', 'trend', 'breakdowns', 'comparison'],
  compareDimension: state.value.compareDimension, compareIds: state.value.compareIds,
  costBasis: remote.value ? 'remote' : 'local',
  ...(remote.value ? { harnesses: [], groupIds: [], versionIds: [], modelKeys: [], compareIds: [], sections: ['accounts'] as const } : {}),
}));
const key = computed(() => JSON.stringify(parameters.value));
const query = useQuery('overview.dimensions', signal => {
  const bounds = props.resolveRange?.() ?? props.range;
  return readAnalysis(bridge, { ...parameters.value, start: bounds.start, end: bounds.end }, signal);
}, {
  deps: [key, () => props.refreshToken], clearOnChange: true, coalesce: true, eventIntervalMs: 60000,
  events: [
    { name: 'usage.changed', when: event => !parameters.value.harnesses?.length || parameters.value.harnesses.some(source => event.harnesses.includes(source)) },
    { name: 'wallet.changed', when: () => remote.value },
    { name: 'quality.changed', when: () => props.kind === 'quality' },
    'pricing.changed',
    'cost.changed',
  ],
});
const snapshot = computed(() => query.data.value);
const integer = (value?: string | null) => value == null ? '—' : BigInt(value).toLocaleString('en-US');
const rate = (value?: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const decimal = (value?: string | null) => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: 1 });
const lookup = (kind: 'vendors' | 'groups' | 'versions' | 'models', id: string) => props.options?.[kind].find(row => row.id === id)?.name ?? (id || (kind === 'vendors' ? '未关联' : '信息缺失'));
function pointLabel(point: CommandData<'stats.explore'>['totals']) { return metric.value === 'hitRate' ? rate(point.cacheHitRate) : metric.value === 'value' ? `${decimal(point.tokensPerDollar)} Token / $1` : metric.value === 'cost' ? formatMoney(point.picoUsd, 'usd4') : integer(point.totalTokens); }
function pointValue(point: CommandData<'stats.explore'>['totals'], line = false) {
  return metric.value === 'hitRate' ? point.cacheHitRate === null ? null : point.cacheHitRate * (line ? 100 : 1)
    : metric.value === 'value' ? point.tokensPerDollar === null ? null : Number(point.tokensPerDollar)
      : Number(metric.value === 'cost' ? point.picoUsd : point.totalTokens) / (line && metric.value === 'cost' ? 1e12 : 1);
}
const points = computed(() => snapshot.value?.trend.map(point => ({ label: point.label, value: pointValue(point), valueLabel: pointLabel(point) })) ?? []);
const dimensionLabel = { vendor: '供应商', group: '价格组', version: '倍率版本', model:'模型' };
const optionKind = { vendor: 'vendors', group: 'groups', version: 'versions', model:'models' } as const;
const series = computed<LineSeries[]>(() => (snapshot.value?.comparisons ?? []).map(row => ({ key: row.id, name: lookup(optionKind[state.value.compareDimension], row.id), points: row.trend.map(point => ({ day: point.bucket, value: pointValue(point, true), label: pointLabel(point) })) })));
const selecting = ref(false), compareSearch = ref(''), compareOffset = ref(0);
const draftDimension = ref<AnalysisView['compareDimension']>(state.value.compareDimension), draftIds = ref<string[]>([]);
watch([compareSearch, draftDimension], () => { compareOffset.value = 0; });
async function openComparison() {
  draftDimension.value = state.value.compareDimension; draftIds.value = [...state.value.compareIds]; compareSearch.value = ''; selecting.value = true;
  await nextTick(); root.value?.querySelector<HTMLInputElement>('.compare-picker>input')?.focus();
}
const compareOptions = computed(() => [{ id: '', name: '信息缺失' }, ...(props.options?.[optionKind[draftDimension.value]] ?? [])]
  .filter(row => row.name.toLowerCase().includes(compareSearch.value.toLowerCase())));
function toggleCompare(id: string, checked: boolean) { draftIds.value = checked ? [...new Set([...draftIds.value, id])].slice(0, 5) : draftIds.value.filter(value => value !== id); }
let restoreComparisonFocus = false;
function restoreComparison() {
  const chart = root.value?.querySelector<HTMLElement>('.explore-chart');
  if (!restoreComparisonFocus || !chart) return;
  restoreComparisonFocus = false; chart.scrollIntoView?.({block:'start'});
  chart.querySelector<HTMLButtonElement>('[data-compare-trigger]')?.focus({preventScroll:true});
}
watch(snapshot, () => { void nextTick(restoreComparison); });
async function closeComparison() {
  selecting.value = false; restoreComparisonFocus = true; await nextTick(); restoreComparison();
}
function finishComparison() { state.value.compareDimension = draftDimension.value; state.value.compareIds = [...draftIds.value]; void closeComparison(); }
const pendingAck = ref(false), ackError = ref('');
async function acknowledge(items = snapshot.value?.quality.filter(item => !item.acknowledged) ?? []) {
  pendingAck.value = true; ackError.value = '';
  try {
    const result = unwrap(await bridge.call('stats.acknowledgeQuality', { items: items.map(({ id, fingerprint }) => ({ id, fingerprint, revision: snapshot.value?.revision })) }));
    if (result.conflicts) ackError.value = `${result.conflicts} 组证据已更新，请核对新内容后确认。`;
    await query.refresh();
  } catch (error) { ackError.value = error instanceof Error ? error.message : '确认缺失失败，请重试'; }
  finally { pendingAck.value = false; }
}
const methods: Record<string, string> = { remote_usage: '远程累计增量', balance_estimate: '余额推算', balance_unresolved: '余额推算，差额未明', legacy_local: '旧版本地估算', local_estimate: '本地估算' };
const statuses = { matched: '误差范围内', review: '待核对', estimated: '推算值，供参考', collecting: '采集尚未稳定', coverage_incomplete: '本地覆盖不足' };
const correctionKinds: Record<string, string> = {consumption_correction:'累计消费更正',balance_correction:'余额校准',correction:'流水类型更正'};
</script>

<template>
  <section ref="root" class="dimension-explorer" :aria-busy="query.loading.value || query.refreshing.value" @keydown.esc="selecting && closeComparison()">
    <header><div><Button v-if="focus !== null && initialModel === undefined && !remote" @click="focus = null">返回模型排名</Button><h2>{{ kind === 'quality' ? '缺失记录' : remote ? '远程消费 C · 账户区间' : focus !== null ? `${snapshot?.models[0]?.displayName ?? '未知模型'} · 模型详情` : kind === 'cache' ? '供应商与模型缓存表现' : kind === 'value' ? '模型性价比' : '本地估算 E' }}</h2></div><Button :pending="query.refreshing.value" @click="query.refresh">更新{{ kind === 'statistics' ? '统计' : '分析' }}</Button></header>
    <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
    <p v-if="query.loading.value" class="text-muted">正在按所选条件加载…</p>
    <section v-if="selecting" class="surface compare-picker" aria-label="选择供应商价格组比较">
      <header><h3>选择比较对象</h3><Button @click="closeComparison">取消</Button></header>
      <ChoiceMenu v-model="draftDimension" label="比较维度" :options="[{value:'vendor',label:'供应商'}, {value:'group',label:'价格组'}, {value:'version',label:'倍率版本'}]" @update:model-value="draftIds = []" />
      <p class="text-muted">比较整个{{ dimensionLabel[draftDimension] }}，沿用当前时间和筛选范围。选完后一次应用，最多五项。</p>
      <input v-model="compareSearch" aria-label="搜索比较对象" placeholder="搜索名称" />
      <div class="compare-options"><label v-for="choice in compareOptions.slice(compareOffset, compareOffset + 50)" :key="choice.id"><input type="checkbox" :checked="draftIds.includes(choice.id)" :disabled="draftIds.length >= 5 && !draftIds.includes(choice.id)" @change="toggleCompare(choice.id, ($event.target as HTMLInputElement).checked)" />{{ choice.name }}</label></div>
      <footer><span>{{ draftIds.length }} / 5 项 · 共 {{ compareOptions.length }} 个候选</span><Button v-if="compareOffset" @click="compareOffset -= 50">上一页</Button><Button v-if="compareOffset + 50 < compareOptions.length" @click="compareOffset += 50">下一页</Button><Button variant="primary" @click="finishComparison">完成选择</Button></footer>
    </section>
    <template v-else-if="snapshot">
      <p class="as-of text-muted">{{ formatDateTime(snapshot.generatedAt, mode) }} 更新 · {{ snapshot.businessTimezone }} · 当天数据尚未完整</p>
      <p v-for="item in snapshot.coverage" :key="item.code" class="capability text-muted">{{ item.message }}</p>
      <template v-if="kind === 'quality'">
        <p class="text-muted">确认只关闭这一组证据的提醒，保留统计及缺失原因。账户差额请在远程消费 C 中核对。</p>
        <div class="quality-actions"><label><input v-model="showAcknowledged" type="checkbox" />显示已确认记录</label><Button :pending="pendingAck" :disabled="!snapshot.quality.some(row => !row.acknowledged)" @click="acknowledge()">确定已知有缺失，关闭本页提醒</Button><span>{{ snapshot.qualityTotal }} 组 · {{ snapshot.qualityUnacknowledged }} 组未确认</span></div>
        <InlineAlert v-if="ackError" severity="danger">{{ ackError }}</InlineAlert>
        <div class="table-wrap"><table aria-label="缺失记录列表"><thead><tr><th>日期</th><th>来源 / 模型</th><th>供应商</th><th>缺失信息</th><th>记录数</th><th>状态</th></tr></thead><tbody><tr v-for="item in snapshot.quality" :key="`${item.id}:${item.fingerprint}`"><td>{{ item.date }}</td><td>{{ item.harness }} / {{ item.displayName }}</td><td>{{ lookup('vendors', item.vendorId) }}</td><td>{{ item.reasons.join('、') }}</td><td>{{ item.countAccuracy === 'at_least' ? '至少 ' : '' }}{{ integer(item.recordCount) }}</td><td><span v-if="item.acknowledged">已确认</span><Button v-else :pending="pendingAck" @click="acknowledge([item])">已知缺失，关闭提醒</Button></td></tr><tr v-if="!snapshot.quality.length"><td colspan="6">当前条件没有{{ showAcknowledged ? '' : '未确认的' }}缺失记录</td></tr></tbody></table></div>
        <footer><Button v-if="qualityOffset" @click="qualityOffset = Math.max(0, qualityOffset - 100)">上一页</Button><Button v-if="snapshot.qualityNextOffset !== null" @click="qualityOffset = snapshot.qualityNextOffset!">下一页</Button></footer>
      </template>
      <template v-else-if="remote">
        <div class="account-summary"><span>所选日期的有效账户消费</span><strong><Money :pico="snapshot.remoteCostPico" /></strong><span>{{ snapshot.accountTotal }} 个完整区间 <Money :pico="snapshot.intervalCostPico" />，独立更正 <Money :pico="snapshot.unallocatedCostPico" /></span></div>
        <p class="text-muted">对照采用当前本地估算；迟到数据只更新核对状态。只有可靠累计、采集稳定、本地覆盖充分且正差额超过 $0.01 时列为待核对。</p>
        <div class="table-wrap"><table aria-label="远程账户区间"><thead><tr><th>供应商 / 区间</th><th>核算方法</th><th>有效消费 C</th><th>结算时 E</th><th>当前 E</th><th>C − 当前 E</th><th>核对状态</th></tr></thead><tbody><tr v-for="row in snapshot.accounts" :key="row.id"><td>{{ lookup('vendors', row.vendorId) }}<small>{{ formatDateTime(row.start, mode) }}<br />至 {{ formatDateTime(row.end, mode) }}</small></td><td>{{ methods[row.method] ?? row.method }}</td><td><Money :pico="row.costPico" /><small v-if="row.originalCostPico !== row.costPico">原额 {{ formatMoney(row.originalCostPico, 'usd4') }}</small></td><td><Money :pico="row.bookedEstimatePico" /></td><td><Money :pico="row.currentEstimatePico" /></td><td><Money :pico="row.differencePico" /></td><td :class="{'review-status':row.status === 'review'}">{{ statuses[row.status] }}</td></tr><tr v-if="!snapshot.accounts.length"><td colspan="7">当前范围没有已结账户区间，可扩大时间范围。</td></tr></tbody></table></div>
        <footer><span>{{ accountOffset + (snapshot.accounts.length ? 1 : 0) }}–{{ accountOffset + snapshot.accounts.length }} / {{ snapshot.accountTotal }}</span><Button v-if="accountOffset" @click="accountOffset = Math.max(0, accountOffset - 100)">上一页</Button><Button v-if="snapshot.accountNextOffset !== null" @click="accountOffset = snapshot.accountNextOffset!">下一页</Button></footer>
        <template v-if="snapshot.accountCorrectionTotal">
          <h3>未绑定账户区间的更正</h3>
          <p class="text-muted">这些更正计入当前所选日期的账户消费，不参与单个区间与本地 E 的比较。</p>
          <div class="table-wrap"><table aria-label="远程账户独立更正"><thead><tr><th>供应商 / 日期</th><th>类型</th><th>消费变动</th><th>说明</th></tr></thead><tbody><tr v-for="row in snapshot.accountCorrections" :key="row.id"><td>{{ lookup('vendors', row.vendorId) }}<small>{{ formatDateTime(row.occurredAt, mode) }}</small></td><td>{{ correctionKinds[row.kind] ?? row.kind }}</td><td><Money :pico="row.costPico" /></td><td class="correction-note">{{ row.note }}</td></tr></tbody></table></div>
          <footer><span>{{ correctionOffset + 1 }}–{{ correctionOffset + snapshot.accountCorrections.length }} / {{ snapshot.accountCorrectionTotal }}</span><Button v-if="correctionOffset" @click="correctionOffset = Math.max(0, correctionOffset - 100)">上一页更正</Button><Button v-if="snapshot.accountCorrectionNextOffset !== null" @click="correctionOffset = snapshot.accountCorrectionNextOffset!">下一页更正</Button></footer>
        </template>
      </template>
      <template v-else>
        <div class="explore-metrics"><article><span>总 Token</span><strong>{{ integer(snapshot.totals.totalTokens) }}</strong><small>输入 {{ integer(snapshot.totals.inputTokens) }} / 输出 {{ integer(snapshot.totals.outputTokens) }}</small></article><article><span>本地估算成本</span><strong><Money :pico="snapshot.totals.picoUsd" /></strong><small>{{ integer(snapshot.totals.recordCount) }} 条请求</small></article><article><span>缓存命中率</span><strong>{{ rate(snapshot.totals.cacheHitRate) }}</strong><small>按已知输入加权</small></article><article v-if="kind === 'value' || focus !== null"><span>每 $1 可用 Token</span><strong>{{ decimal(snapshot.totals.tokensPerDollar) }}</strong><small>仅计入完整、已定价样本</small></article></div>
        <section class="surface explore-chart">
          <header><h3>{{ focus !== null ? '模型历史趋势' : '趋势与比较' }}</h3><div class="chart-controls">
            <ChoiceMenu v-model="metric" label="趋势指标" :options="[{value:'tokens',label:'Token'}, {value:'cost',label:'本地成本'}, {value:'hitRate',label:'缓存命中率'}, {value:'value',label:'Token / 美元'}]" />
            <ChoiceMenu v-if="kind === 'statistics'" v-model="grain" label="时间粒度" :options="[{value:'day',label:'按天'}, {value:'hour',label:'小时明细'}]" />
            <Button data-compare-trigger @click="openComparison">比较{{ dimensionLabel[state.compareDimension] }}</Button><Button v-if="state.compareIds.length" @click="state.compareIds = []">清除比较</Button>
          </div></header>
          <MultiLineChart v-if="state.compareIds.length" :series="series" label="供应商和倍率版本比较趋势" :unit="metric === 'hitRate' ? '%' : metric === 'cost' ? ' USD' : ''" />
          <BarChart v-else :points="points" :y-format="metric === 'hitRate' ? 'percent' : metric === 'cost' ? 'cost' : 'tokens'" :variant="metric === 'hitRate' ? 'line' : 'bar'" label="筛选统计趋势" :x-tick-count="6" />
        </section>
        <UsageComposition v-if="offset === 0" :snapshot="snapshot" />
        <ThroughputReference v-if="focus !== null" :scope="{...scope, modelKeys:[focus]}" :options="options" />
        <section v-if="kind === 'value' || focus !== null" class="value-parts surface"><h3>每美元用量结构</h3><div><article v-for="(label, part) in { input: '未命中输入', cache: '缓存命中', output: '输出', write: '缓存创建' }" v-show="part !== 'write' || snapshot.totals.parts.write !== '0'" :key="part"><span>{{ label }}</span><strong>{{ decimal(snapshot.totals.perDollar[part]) }}</strong></article></div><p class="text-muted">{{ formatMoney(snapshot.totals.eligibleCostPico, 'usd4') }} 纳入计算；按历史完整计费样本展示，不代表模型质量。</p></section>
        <section class="surface explore-table"><header><h3>模型{{ focus !== null ? '用量' : '排名' }} · 本地估算成本</h3><span>{{ snapshot.totalModels }} 个模型</span></header><div class="table-wrap"><table aria-label="筛选模型排名"><thead><tr><th>模型</th><th>Token</th><th>本地估算</th><th>缓存命中率</th><th>Token / $1</th><th></th></tr></thead><tbody><tr v-for="model in snapshot.models" :key="model.modelKey"><td>{{ model.displayName }}</td><td>{{ integer(model.totalTokens) }}</td><td><Money :pico="model.picoUsd" /></td><td>{{ rate(model.cacheHitRate) }}</td><td>{{ decimal(model.tokensPerDollar) }}</td><td><Button v-if="focus === null" @click="focus = model.modelKey">模型详情</Button></td></tr><tr v-if="!snapshot.models.length"><td colspan="6">当前筛选交集为零，可减少条件或扩大时间范围。</td></tr></tbody></table></div><footer><Button v-if="offset" @click="offset = Math.max(0, offset - 50)">上一页</Button><Button v-if="snapshot.nextOffset !== null" @click="offset = snapshot.nextOffset!">下一页</Button></footer></section>
        <details class="surface explore-table"><summary>查看供应商、价格组及倍率版本明细（最多前 100 项）</summary><div class="table-wrap"><table aria-label="供应商版本比较"><thead><tr><th>供应商</th><th>价格组</th><th>倍率版本</th><th>来源 / 模型</th><th>Token</th></tr></thead><tbody><tr v-for="(row,index) in snapshot.breakdowns" :key="index"><td>{{ lookup('vendors', row.vendorId) }}</td><td>{{ lookup('groups', row.groupId) }}</td><td>{{ lookup('versions', row.versionId) }}</td><td>{{ row.harness }} / {{ row.displayName }}</td><td>{{ integer(row.totalTokens) }}</td></tr></tbody></table></div></details>
      </template>
    </template>
  </section>
</template>

<style scoped>
.dimension-explorer{display:grid;gap:18px;min-width:0}header,footer,.chart-controls,.quality-actions{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}h2{font-size:19px}h3{font-size:15px}header>span,.as-of,.capability,.quality-actions,p,summary{font-size:12px;line-height:1.7}.explore-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:20px;border-block:1px solid var(--border);padding:22px 0}.explore-metrics article,.value-parts article{display:grid;gap:10px}.explore-metrics strong{font-size:24px;font-weight:600;overflow-wrap:anywhere}.explore-metrics span,.explore-metrics small,.value-parts small{font-size:12px;color:var(--text-muted)}.explore-chart,.explore-table,.value-parts,.compare-picker{padding:20px;display:grid;gap:16px;min-width:0}.explore-chart{min-height:350px}.value-parts>div{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px}.value-parts strong{font-size:21px;color:var(--money)}.value-parts span{font-size:13px}.table-wrap{overflow:auto;max-height:470px;border:1px solid var(--border);border-radius:5px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:11px 12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border)}th{position:sticky;top:0;background:var(--surface-2);z-index:1}footer{justify-content:flex-end}footer>span{margin-right:auto;font-size:12px}.quality-actions label,.compare-options label{display:flex;align-items:center;gap:8px}input[type=checkbox]{min-height:0}.compare-options{display:grid;gap:6px;max-height:400px;overflow:auto}.compare-options label{padding:9px;border-bottom:1px solid var(--border);font-size:13px}.compare-picker>input{padding:9px;max-width:450px}.account-summary{display:grid;gap:12px;border-left:3px solid var(--money);padding:12px 18px;font-size:13px}.account-summary strong{font-size:28px}td small{display:block;font-size:11px;color:var(--text-muted);line-height:1.7;margin-top:5px}.review-status{color:var(--warn);font-weight:600}summary{cursor:pointer}
</style>
<style scoped>
.correction-note{white-space:normal;min-width:180px;max-width:480px;overflow-wrap:anywhere}
</style>
