<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { readShared } from '../../shared/readService';
import type { UsageRange } from '../usage-common/types';
import type { AnalysisView } from './analysisState';
import { useQuery } from '../../shared/query';
import type { ExploreScope } from './scope';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import MultiLineChart, { type LineSeries } from '../../ui/charts/MultiLineChart.vue';
import { useModelOptions } from './useModelOptions';
const props = defineProps<{ snapshot?: CommandData<'stats.analytics'>; scope?: ExploreScope; options?: CommandData<'stats.exploreOptions'>; refreshToken?: number; range?:UsageRange; resolveRange?:() => UsageRange; view?:AnalysisView }>();
const bridge = inject(BridgeKey)!;
const emit = defineEmits<{ selecting: [value:boolean] }>();
const localModel = ref<string|null>(null);
const model = computed({get:() => props.view?.tpsModel ?? localModel.value,set:value => { if(props.view) props.view.tpsModel = value; else localModel.value = value; }});
const draftModel = ref<string | null>(null);
const root = ref<HTMLElement | null>(null);
const localCompared = ref<string[]>([]);
const compared = computed({get:() => props.view?.tpsCompareIds ?? localCompared.value,set:value => {if(props.view) props.view.tpsCompareIds = value; else localCompared.value = value;}});
const draftCompared = ref<string[]>([]);
const selecting = ref(false);
watch(selecting, value => {
  if (value) { draftCompared.value = [...compared.value]; draftModel.value = model.value; void nextTick(() => root.value?.querySelector<HTMLInputElement>('.search')?.focus()); }
  emit('selecting', value);
});
const models = useModelOptions(selecting);
const catalog = ref<Record<string, string>>({});
function remember(values: {id:string;name:string}[]) {
  for (const row of values) catalog.value[row.id] = row.name;
  const keep = new Set([model.value, draftModel.value, ...compared.value, ...draftCompared.value, ...(props.options?.models ?? []).map(row => row.id)]);
  for (const key of Object.keys(catalog.value)) if (Object.keys(catalog.value).length > 300 && !keep.has(key)) delete catalog.value[key];
}
watch(() => props.options ? [...props.options.models,...(props.options.selectedModels ?? [])] : props.snapshot?.models.map(row => ({id:row.modelKey,name:row.displayName})), values => remember(values ?? []), {immediate:true});
watch(models.rows, remember);
const choices = computed(() => Object.entries(catalog.value).map(([value,label]) => ({value,label})).filter(row => !props.scope?.modelKeys?.length || props.scope.modelKeys.includes(row.value)));
watch(choices, values => { if (!values.some(row => row.value === model.value)) model.value = values[0]?.value ?? null; compared.value = compared.value.filter(value => values.some(row => row.value === value) && value !== model.value); }, { immediate: true });
watch(model, value => { compared.value = compared.value.filter(key => key !== value); });
const keys = computed(() => model.value === null ? [] : [model.value, ...compared.value].slice(0, 5));
const parameters = computed(() => ({ ...(props.range ? {start:props.range.start,end:props.range.end} : {}), harnesses: props.scope?.harnesses ?? [], vendorIds: props.scope?.vendorIds ?? [], groupIds: props.scope?.groupIds ?? [], versionIds: props.scope?.versionIds ?? [], modelKeys: keys.value }));
const key = computed(() => JSON.stringify(parameters.value));
const query = useQuery('overview.outputReferences', signal => { const range = props.resolveRange?.(); return readShared(bridge,'stats.outputReferences',{...parameters.value,...(range ? {start:range.start,end:range.end} : {})},signal); }, { deps: [key, () => props.refreshToken], visible: () => !selecting.value && model.value !== null, clearOnChange: true, coalesce: true, events: ['output.changed'] });
const today = computed(() => (query.data.value?.todayReferences ?? query.data.value?.references)?.find(row => row.day === query.data.value?.today && row.modelKey === model.value));
const name = computed(() => choices.value.find(row => row.value === model.value)?.label ?? '模型');
const days = computed(() => {
  const data = query.data.value; if (!data) return [];
  const day = (stamp:string) => new Intl.DateTimeFormat('en-CA',{timeZone:data.businessTimezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(stamp));
  const first = data.start ? day(data.start) : new Date(Date.parse(`${data.today}T00:00:00Z`) - 29 * 86400000).toISOString().slice(0,10);
  const last = data.end ? day(new Date(Date.parse(data.end) - 1).toISOString()) : data.today;
  const count = Math.round((Date.parse(last) - Date.parse(first))/86400000) + 1;
  return count > 120 ? undefined : Array.from({length:Math.max(0,count)},(_,index) => new Date(Date.parse(`${first}T00:00:00Z`) + index*86400000).toISOString().slice(0,10));
});
const series = computed<LineSeries[]>(() => keys.value.map(value => ({ key: value, name: choices.value.find(row => row.value === value)?.label ?? value, points: (query.data.value?.references ?? []).filter(row => row.modelKey === value).map(row => ({ day: row.day, value: row.tokensPerSecond === null ? null : Number(row.tokensPerSecond), label: `${row.tokensPerSecond ?? '—'} t/s · ${row.samples} 个会话样本` })) })));
function toggle(value: string, checked: boolean) { draftCompared.value = checked ? [...draftCompared.value, value].slice(0, 4) : draftCompared.value.filter(key => key !== value); }
async function close() { selecting.value = false; await nextTick(); root.value?.querySelector<HTMLButtonElement>('[data-throughput-compare]')?.focus({preventScroll:true}); root.value?.scrollIntoView?.({block:'start'}); }
function finish() { model.value = draftModel.value; compared.value = draftCompared.value.filter(key => key !== model.value); void close(); }
</script>

<template>
  <section ref="root" class="surface throughput-reference" aria-label="输出吞吐参考" @keydown.esc="selecting && close()">
    <template v-if="selecting">
      <header><h2>选择比较模型</h2><Button @click="close">返回吞吐参考</Button></header>
      <p class="text-muted">{{ name }}为当前模型，最多再添加四个模型。颜色仅用于本次比较。</p>
      <input v-model="models.search.value" class="search" aria-label="搜索吞吐比较模型" placeholder="搜索模型名称" />
      <div class="comparison-models"><div v-for="choice in models.query.data.value ? models.rows.value.filter(row => !scope?.modelKeys?.length || scope.modelKeys.includes(row.id)).map(row => ({value:row.id,label:row.name})) : choices" :key="choice.value">
        <label><input type="checkbox" :checked="choice.value === draftModel || draftCompared.includes(choice.value)" :disabled="choice.value === draftModel || (draftCompared.length >= 4 && !draftCompared.includes(choice.value))" @change="toggle(choice.value, ($event.target as HTMLInputElement).checked)" />{{ choice.label }}<span v-if="choice.value === draftModel">当前模型</span></label>
        <Button v-if="choice.value !== draftModel" @click="draftModel = choice.value; draftCompared = draftCompared.filter(key => key !== choice.value)">设为当前模型</Button>
      </div></div>
      <footer><span>{{ draftCompared.length }} / 4 个比较模型 · {{ models.total.value }} 个候选</span><Button v-if="models.offset.value" @click="models.offset.value -= 100">上一页</Button><Button v-if="models.next.value !== null" @click="models.offset.value = models.next.value!">下一页</Button><Button @click="finish">完成选择</Button></footer>
    </template>
    <template v-else>
      <header><h2>输出吞吐参考</h2><div><ChoiceMenu v-if="choices.length" :model-value="model ?? ''" label="吞吐参考模型" :options="choices" @update:model-value="model = $event" /><Button data-throughput-compare @click="selecting = true">比较模型</Button></div></header>
      <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
      <p v-for="item in query.data.value?.coverage ?? []" :key="item.code" class="text-muted">{{ item.message }}</p>
      <div class="daily-speed"><span>{{ name }} · 今日平均tps</span><strong>{{ today?.tokensPerSecond ?? '—' }} <small>t/s</small></strong><span>{{ today ? `${today.samples} 个已保存会话样本` : '今天尚无已保存会话样本' }}</span></div>
      <p class="text-muted">{{ range ? '按本页独立时间范围展示；' : '近三十个业务日；' }}静默十分钟后保存样本。同一会话的全部吞吐归入其开始日，续聊更新原样本。今日无样本时不使用其他日期或当前会话数值替代。</p>
      <MultiLineChart :series="series" :days="days" label="每日模型输出吞吐比较" unit=" t/s" />
      <p v-if="scope?.vendorIds?.length || scope?.groupIds?.length || scope?.versionIds?.length" class="text-muted">当前供应商与版本筛选仅包含保留请求归属证据的已发布样本；维度沿用采样时原归属，历史重算不改写。旧版已丢失请求身份的参考值保留在普通曲线，不推测分配。</p>
    </template>
  </section>
</template>

<style scoped>
.throughput-reference{padding:22px;display:grid;gap:18px;min-width:0}header,header>div,footer{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}h2{font-size:18px}p{font-size:12px;line-height:1.8}.daily-speed{display:grid;gap:9px;border-left:3px solid var(--accent);padding-left:16px}.daily-speed>span{font-size:13px;color:var(--text-muted)}.daily-speed strong{font-size:30px;font-weight:600;color:var(--money)}small{font-size:15px;font-weight:400}.search{padding:9px 12px;width:min(100%,450px)}.comparison-models{display:grid;gap:4px;max-height:400px;overflow:auto}.comparison-models label{display:flex;align-items:center;gap:12px;padding:10px;border-bottom:1px solid var(--border);font-size:14px}.comparison-models input{min-height:0}.comparison-models span,footer>span{font-size:12px;color:var(--text-muted)}
</style>
<style scoped>
.throughput-reference{padding:16px;gap:12px}h2{font-size:17px}.daily-speed{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px 16px;padding-left:12px;width:fit-content}.daily-speed strong{font-size:23px}.daily-speed small{font-size:13px}p{line-height:1.6}
</style>
<style scoped>
.comparison-models>div{display:flex;align-items:center;justify-content:space-between;gap:12px}.comparison-models label{min-width:0;overflow-wrap:anywhere;flex:1}.comparison-models>div>button{flex-shrink:0}
</style>
