<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { useQuery } from '../../shared/query';
import { readAnalysis } from './analysisQuery';
import type { ExploreScope } from './scope';
import type { UsageRange } from '../usage-common/types';
import type { AnalysisView } from './analysisState';
import { useModelOptions } from './useModelOptions';
import { millionTokens } from './valueFormat';
import { priceGroupOptions, priceVersionOptions } from './comparisonOptions';
import UsageComposition from './UsageComposition.vue';
import MultiLineChart, { type LineSeries } from '../../ui/charts/MultiLineChart.vue';
import Button from '../../ui/Button.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Money from '../../ui/Money.vue';
import { formatMoney } from '../../shared/money';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
const props = defineProps<{mode?:TimeZoneMode;theme:'cost'|'cache'|'value';scope:ExploreScope;range:UsageRange;rangeKind:RequestMap['stats.explore']['rangeKind'];resolveRange:() => UsageRange;view:AnalysisView;options?:CommandData<'stats.exploreOptions'>;refreshToken?:number}>();
const bridge = inject(BridgeKey)!, offset = ref(0), selecting = ref(false), root = ref<HTMLElement>();
const draftDimension = ref<'model'|'group'|'version'>(props.view.compareDimension === 'vendor' ? 'model' : props.view.compareDimension), draftIds = ref<string[]>([]), search = ref('');
const comparison = ref({dimension:props.view.compareDimension,ids:[...props.view.compareIds]});
const modelOptions = useModelOptions(computed(() => selecting.value && draftDimension.value === 'model'));
const parameters = computed<RequestMap['stats.explore']>(() => ({...props.scope,start:props.range.start,end:props.range.end,rangeKind:props.rangeKind,sections:['totals','models','trend',...(comparison.value.ids.length ? ['comparison' as const] : [])],grain:'day',offset:offset.value,limit:50,costBasis:'local',compareDimension:comparison.value.ids.length ? comparison.value.dimension : 'model',compareIds:comparison.value.ids}));
watch(() => JSON.stringify([props.scope,props.range,props.theme]),() => {offset.value = 0;});
const query = useQuery('analysis.theme',signal => {const range = props.resolveRange(); return readAnalysis(bridge,{...parameters.value,start:range.start,end:range.end},signal);},{deps:[() => JSON.stringify(parameters.value),() => props.refreshToken],clearOnChange:true,coalesce:true,eventIntervalMs:60000,events:[{name:'usage.changed',when:event => !props.scope.harnesses?.length || props.scope.harnesses.some(h => event.harnesses.includes(h))},{name:'cost.changed',when:() => props.theme !== 'cache'}]});
const data = computed(() => query.data.value);
const title = computed(() => ({cost:'本地有效成本',cache:'缓存命中率',value:'模型性价比'}[props.theme]));
type Metrics = CommandData<'stats.explore'>['totals'];
const label = (row:Metrics) => props.theme === 'cost' ? formatMoney(row.picoUsd,'usd4') : props.theme === 'cache' ? row.cacheHitRate == null ? '—' : `${(row.cacheHitRate*100).toFixed(1)}%` : `${millionTokens(row.tokensPerDollar)} M Token/USD（${row.tokensPerDollar ?? '未知'} Token/USD）`;
const value = (row:Metrics) => props.theme === 'cost' ? Number(row.picoUsd)/1e12 : props.theme === 'cache' ? row.cacheHitRate == null ? null : row.cacheHitRate*100 : row.tokensPerDollar == null ? null : Number(row.tokensPerDollar)/1e6;
const groups = computed(() => priceGroupOptions(props.options)), versions = computed(() => priceVersionOptions(props.options));
const optionName = (id:string) => (comparison.value.dimension === 'model' ? [...(props.options?.models ?? []),...(props.options?.selectedModels ?? []),...modelOptions.rows.value] : comparison.value.dimension === 'group' ? groups.value : versions.value)?.find(row => row.id === id)?.name ?? (id || '信息缺失');
const totalLabel = computed(() => props.scope.modelKeys?.length ? '所选模型总和' : '全部模型总和');
const series = computed<LineSeries[]>(() => !data.value ? [] : comparison.value.ids.length ? data.value.comparisons.map(row => ({key:row.id,name:optionName(row.id),points:row.trend.map(point => ({day:point.bucket,value:value(point),label:label(point)}))})) : [{key:'total',name:totalLabel.value,points:data.value.trend.map(point => ({day:point.bucket,value:value(point),label:label(point)}))}]);
const candidates = computed(() => (draftDimension.value === 'model' ? modelOptions.rows.value : [{id:'',name:'信息缺失'},...(draftDimension.value === 'group' ? groups.value : versions.value)]).filter(row => row.name.toLowerCase().includes(search.value.toLowerCase())));
const structure = ref('total');
const structureRows = computed(() => !data.value ? [] : comparison.value.ids.length ? data.value.comparisons.map(row => ({key:`compare:${row.id}`,name:optionName(row.id),metrics:row.totals})) : data.value.models.map(row => ({key:`model:${row.modelKey}`,name:row.displayName,metrics:row})));
const structureChoices = computed(() => [{value:'total',label:totalLabel.value},...structureRows.value.map(row => ({value:row.key,label:row.name}))]);
const structureRow = computed(() => structureRows.value.find(row => row.key === structure.value));
const structureLabel = computed(() => structureRow.value?.name ?? totalLabel.value);
const structureMetrics = computed(() => structureRow.value?.metrics ?? data.value?.totals);
const structureSnapshot = computed(() => data.value && structureMetrics.value ? {...data.value,totals:structureMetrics.value} : undefined);
watch(structureChoices, rows => {if(!rows.some(row => row.value === structure.value))structure.value='total';});
watch(search, value => {modelOptions.search.value = value;});
watch(draftDimension,() => {draftIds.value = []; search.value = '';});
async function openComparison() {draftDimension.value = props.view.compareDimension === 'vendor' ? 'model' : props.view.compareDimension; await nextTick(); draftIds.value = [...props.view.compareIds]; selecting.value = true; await nextTick(); root.value?.querySelector<HTMLInputElement>('.comparison-picker>input')?.focus();}
async function closeComparison() {selecting.value = false; await nextTick(); root.value?.querySelector<HTMLButtonElement>('[data-compare]')?.focus({preventScroll:true});}
function applyComparison() {props.view.compareDimension = draftDimension.value; props.view.compareIds = [...draftIds.value]; comparison.value = {dimension:draftDimension.value,ids:[...draftIds.value]}; void closeComparison();}
function clearComparison() {props.view.compareIds=[];comparison.value={...comparison.value,ids:[]};}
const integer = (value:string) => BigInt(value).toLocaleString('en-US');
</script>
<template><section ref="root" class="analysis-theme" :aria-label="title">
  <header><h2>{{ title }}</h2><div><Button :aria-pressed="!comparison.ids.length" @click="clearComparison">{{totalLabel}}</Button><Button data-compare @click="openComparison">比较模型或价格组</Button><Button v-if="comparison.ids.length" @click="clearComparison">清除比较</Button><Button @click="query.refresh">更新分析</Button></div></header>
  <section v-if="selecting" class="comparison-picker surface" @keydown.esc="closeComparison"><ChoiceMenu v-model="draftDimension" label="比较对象" :options="[{value:'model',label:'模型'},{value:'group',label:'价格组'},{value:'version',label:'倍率版本'}]" /><input v-model="search" aria-label="搜索分析比较对象" placeholder="搜索名称" /><label class="total-choice"><input type="checkbox" :checked="!draftIds.length" @change="draftIds=[]" />{{totalLabel}}</label><div class="candidate-list"><label v-for="row in candidates" :key="row.id"><input type="checkbox" :checked="draftIds.includes(row.id)" :disabled="draftIds.length >= 5 && !draftIds.includes(row.id)" @change="draftIds = ($event.target as HTMLInputElement).checked ? [...new Set([...draftIds,row.id])].slice(0,5) : draftIds.filter(id => id !== row.id)" />{{ row.name }}</label></div><footer><span>{{ draftIds.length }} / 5 项</span><Button v-if="draftDimension === 'model' && modelOptions.offset.value" @click="modelOptions.offset.value -= 100">上一页候选</Button><Button v-if="draftDimension === 'model' && modelOptions.next.value != null" @click="modelOptions.offset.value = modelOptions.next.value!">下一页候选</Button><Button @click="closeComparison">返回分析</Button><Button variant="primary" @click="applyComparison">完成选择</Button></footer></section>
  <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
  <template v-if="data"><p class="as-of">{{ formatDateTime(data.start, mode ?? 'system') }} 至 {{ formatDateTime(data.end, mode ?? 'system') }}</p><InlineAlert v-for="item in data.coverage" :key="item.code">{{ item.message }}</InlineAlert>
    <section class="theme-summary surface"><span>{{totalLabel}}</span><strong :title="theme === 'value' ? `${data.totals.tokensPerDollar ?? '未知'} Token/USD` : label(data.totals)">{{ theme === 'value' ? `${millionTokens(data.totals.tokensPerDollar)} M Token/USD` : label(data.totals) }}</strong><p v-if="theme === 'cost'">原估算 <Money :pico="data.totals.estimatePico ?? data.totals.picoUsd" />；含校准金额 <Money mode="usd4" :pico="data.totals.correctionPico" />；未分配 <Money :pico="data.totals.unallocatedCorrectionPico" />。</p><p v-else-if="theme === 'cache'">缓存命中合计 {{ integer(data.totals.cacheReadTokens) }} / 输入合计 {{ integer(data.totals.inputTokens) }}；仅按缓存字段已知的输入加权。{{ data.totals.cacheCoverageIncomplete ? '当前存在缓存覆盖缺失。' : '' }}</p><p v-else>同样本 {{ integer(data.totals.eligibleTokens) }} Token / <Money :pico="data.totals.eligibleCostPico" />。{{ data.totals.ratioUnavailable ? '校准未明确归属或缺少同样本依据，当前不计算性价比。' : '百万单位，零费用不计算无穷值。' }}</p></section>
    <section class="surface theme-chart"><MultiLineChart :series="series" :label="`${title}趋势比较`" :unit="theme === 'value' ? 'Token/USD' : theme === 'cache' ? '%' : ' USD'" :axis-unit="theme === 'value' ? ' M' : undefined" /><p v-if="theme === 'cost' && data.unallocatedTrendCorrectionPico !== '0'">无法归属到当前趋势粒度的校准：<Money :pico="data.unallocatedTrendCorrectionPico" /></p></section>
    <ChoiceMenu v-if="theme !== 'cost'" v-model="structure" :label="theme === 'cache' ? 'Token 使用结构视图' : '同样本用量结构视图'" :options="structureChoices" />
    <UsageComposition v-if="theme !== 'value' && structureSnapshot && (theme !== 'cost' || offset === 0)" :snapshot="structureSnapshot" :theme="theme" :scope-label="theme === 'cost' ? totalLabel : structureLabel" />
    <section v-if="theme === 'value' && structureMetrics" class="value-parts surface"><h3>同样本用量结构 · {{structureLabel}} · M Token/USD</h3><div><article v-for="(name,key) in {input:'新增输入',cache:'缓存命中',output:'输出',write:'计费缓存创建'}" :key="key" v-show="key !== 'write' || structureMetrics.parts.write !== '0'"><span>{{ name }}</span><strong :title="`${structureMetrics.perDollar[key] ?? '未知'} Token/USD`">{{ millionTokens(structureMetrics.perDollar[key]) }}</strong></article></div><p v-if="structureMetrics.componentCostEstimated">类别费用未单独校准，分项继续标示估算口径，不能据此推断真实分类费率。</p></section>
    <section class="surface model-ranking"><h3>{{ title }} · 模型</h3><div class="table-wrap"><table aria-label="主题模型表"><thead><tr><th>模型</th><th>{{ theme === 'value' ? 'M Token/USD' : title }}</th><template v-if="theme === 'value'"><th v-for="name in ['新增输入','缓存命中','输出','计费缓存创建']" :key="name">{{name}}（M）</th></template><th>覆盖</th></tr></thead><tbody><tr v-for="row in data.models" :key="row.modelKey"><td>{{ row.displayName }}</td><td :title="theme === 'value' ? `${row.tokensPerDollar ?? '未知'} Token/USD` : undefined">{{ theme === 'value' ? millionTokens(row.tokensPerDollar) : label(row) }}</td><template v-if="theme === 'value'"><td v-for="key in (['input','cache','output','write'] as const)" :key="key" :title="`${row.perDollar[key] ?? '未知'} Token/USD`">{{millionTokens(row.perDollar[key])}}</td></template><td>{{ row.ratioUnavailable && theme === 'value' ? '无法归属校准' : row.incomplete || row.unpriced || row.cacheCoverageIncomplete ? '部分样本缺失' : '当前范围' }}</td></tr><tr v-if="!data.models.length"><td :colspan="theme === 'value' ? 7 : 3">当前条件没有模型样本。</td></tr></tbody></table></div><footer><Button v-if="offset" @click="offset = Math.max(0,offset-50)">上一页模型</Button><Button v-if="data.nextOffset !== null" @click="offset = data.nextOffset!">下一页模型</Button></footer></section>
  </template>
</section></template>
<style scoped>
.analysis-theme{display:grid;gap:20px;min-width:0}header,header>div,footer{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}h2{font-size:19px}h3{font-size:15px}.theme-summary,.theme-chart,.model-ranking,.value-parts,.comparison-picker{padding:22px;min-width:0}.theme-summary strong{display:block;font-size:26px;overflow-wrap:anywhere;color:var(--money)}p,.as-of{font-size:12px;color:var(--text-muted);line-height:1.8;margin-top:12px}.as-of{margin:0}.table-wrap{overflow:auto;max-height:460px}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border)}th{position:sticky;top:0;background:var(--surface-2)}footer{justify-content:flex-end;padding-top:14px}.comparison-picker{display:grid;gap:14px}.comparison-picker>input{padding:9px}.candidate-list{max-height:350px;overflow:auto;display:grid;gap:8px}.candidate-list label{display:flex;align-items:center;gap:10px;font-size:13px}.candidate-list input{min-height:0}.value-parts>div{display:flex;gap:28px;flex-wrap:wrap;margin-top:20px}.value-parts article{display:grid;gap:10px;font-size:13px}.value-parts strong{font-size:22px}
</style>
<style scoped>
.analysis-theme{gap:14px}header,header>div,footer{gap:8px}h2{font-size:17px}.theme-summary{width:fit-content;max-width:100%;display:flex;align-items:center;gap:12px 20px;flex-wrap:wrap;padding:12px 16px}.theme-summary strong{font-size:21px;line-height:1.3}.theme-summary p{margin:0;line-height:1.6;max-width:64ch}.theme-chart,.model-ranking,.value-parts,.comparison-picker{padding:16px}.value-parts>div{gap:14px 26px;margin-top:12px}.value-parts article{gap:6px}.value-parts strong{font-size:19px}td,th{padding:9px 12px}footer{padding-top:10px}
</style>
