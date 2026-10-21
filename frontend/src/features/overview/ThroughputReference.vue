<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import type { ExploreScope } from './scope';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import MultiLineChart, { type LineSeries } from '../../ui/charts/MultiLineChart.vue';
const props = defineProps<{ snapshot?: CommandData<'stats.analytics'>; scope?: ExploreScope; options?: CommandData<'stats.exploreOptions'> }>();
const bridge = inject(BridgeKey)!;
const emit = defineEmits<{ selecting: [value:boolean] }>();
const model = ref('');
const compared = ref<string[]>([]);
const selecting = ref(false);
watch(selecting, value => emit('selecting', value));
const search = ref('');
const choices = computed(() => (props.options?.models.map(row => ({ value: row.id, label: row.name })) ?? props.snapshot?.models.map(row => ({ value: row.modelKey, label: row.displayName })) ?? []).filter(row => !props.scope?.modelKeys?.length || props.scope.modelKeys.includes(row.value)));
watch(choices, values => { if (!values.some(row => row.value === model.value)) model.value = values[0]?.value ?? ''; compared.value = compared.value.filter(value => values.some(row => row.value === value) && value !== model.value); }, { immediate: true });
watch(model, value => { compared.value = compared.value.filter(key => key !== value); });
const keys = computed(() => [model.value, ...compared.value].filter(Boolean).slice(0, 5));
const parameters = computed(() => ({ harnesses: props.scope?.harnesses ?? [], vendorIds: props.scope?.vendorIds ?? [], groupIds: props.scope?.groupIds ?? [], versionIds: props.scope?.versionIds ?? [], modelKeys: keys.value }));
const key = computed(() => JSON.stringify(parameters.value));
const query = useQuery('overview.outputReferences', async () => unwrap(await bridge.call('stats.outputReferences', parameters.value)), { deps: [key], clearOnChange: true, coalesce: true, events: ['output.changed'] });
const today = computed(() => query.data.value?.references.find(row => row.day === query.data.value?.today && row.modelKey === model.value));
const name = computed(() => choices.value.find(row => row.value === model.value)?.label ?? '模型');
const days = computed(() => query.data.value ? Array.from({length:30}, (_, index) => new Date(Date.parse(`${query.data.value!.today}T00:00:00Z`) - (29 - index) * 86400000).toISOString().slice(0,10)) : []);
const series = computed<LineSeries[]>(() => keys.value.map(value => ({ key: value, name: choices.value.find(row => row.value === value)?.label ?? value, points: (query.data.value?.references ?? []).filter(row => row.modelKey === value).map(row => ({ day: row.day, value: row.tokensPerSecond === null ? null : Number(row.tokensPerSecond), label: `${row.tokensPerSecond ?? '—'} t/s · ${row.samples} 个会话样本` })) })));
function toggle(value: string, checked: boolean) { compared.value = checked ? [...compared.value, value].slice(0, 4) : compared.value.filter(key => key !== value); }
</script>

<template>
  <section class="surface throughput-reference" aria-label="输出吞吐参考">
    <template v-if="selecting">
      <header><h2>选择比较模型</h2><Button @click="selecting = false">返回吞吐参考</Button></header>
      <p class="text-muted">{{ name }}为当前模型，最多再添加四个模型。颜色仅用于本次比较。</p>
      <input v-model="search" class="search" aria-label="搜索吞吐比较模型" placeholder="搜索模型名称" />
      <div class="comparison-models"><label v-for="choice in choices.filter(row => row.label.toLowerCase().includes(search.toLowerCase())).slice(0, 100)" :key="choice.value"><input type="checkbox" :checked="choice.value === model || compared.includes(choice.value)" :disabled="choice.value === model || (compared.length >= 4 && !compared.includes(choice.value))" @change="toggle(choice.value, ($event.target as HTMLInputElement).checked)" />{{ choice.label }}<span v-if="choice.value === model">当前模型</span></label></div>
      <footer><span>{{ compared.length }} / 4 个比较模型</span><Button @click="selecting = false">完成选择</Button></footer>
    </template>
    <template v-else>
      <header><h2>输出吞吐参考</h2><div><ChoiceMenu v-if="choices.length" v-model="model" label="吞吐参考模型" :options="choices" /><Button @click="selecting = true">比较模型</Button></div></header>
      <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
      <div class="daily-speed"><span>{{ name }} · 今日平均tps</span><strong>{{ today?.tokensPerSecond ?? '—' }} <small>t/s</small></strong><span>{{ today ? `${today.samples} 个已保存会话样本` : '今天尚无已保存会话样本' }}</span></div>
      <p class="text-muted">近三十个业务日；静默十分钟后保存样本。同一会话的全部吞吐归入其开始日，续聊更新原样本。今日无样本时不使用其他日期或当前会话数值替代。</p>
      <MultiLineChart :series="series" :days="days" label="每日模型输出吞吐比较" unit=" t/s" />
      <p v-if="scope?.vendorIds?.length || scope?.groupIds?.length || scope?.versionIds?.length" class="text-muted">当前供应商与版本筛选仅包含保留归属证据的吞吐样本；旧版缺失归属的参考值不推测分配。</p>
    </template>
  </section>
</template>

<style scoped>
.throughput-reference{padding:22px;display:grid;gap:18px;min-width:0}header,header>div,footer{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}h2{font-size:18px}p{font-size:12px;line-height:1.8}.daily-speed{display:grid;gap:9px;border-left:3px solid var(--accent);padding-left:16px}.daily-speed>span{font-size:13px;color:var(--text-muted)}.daily-speed strong{font-size:30px;font-weight:600;color:var(--money)}small{font-size:15px;font-weight:400}.search{padding:9px 12px;width:min(100%,450px)}.comparison-models{display:grid;gap:4px;max-height:400px;overflow:auto}.comparison-models label{display:flex;align-items:center;gap:12px;padding:10px;border-bottom:1px solid var(--border);font-size:14px}.comparison-models input{min-height:0}.comparison-models span,footer>span{font-size:12px;color:var(--text-muted)}
</style>
