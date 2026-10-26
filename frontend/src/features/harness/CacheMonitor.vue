<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue';
import { useCacheMonitor } from './useCacheMonitor';
import type { Harness } from './types';
import type { CommandData } from '../../app/ports';
import type { TimeZoneMode } from '../../shared/time';
import { formatDateTime } from '../../shared/format';
import BarChart from '../../ui/charts/BarChart.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Skeleton from '../../ui/Skeleton.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import Button from '../../ui/Button.vue';
import type { ChartPoint } from '../../ui/charts/axis';
const props = defineProps<{ harness: Harness; timezone: TimeZoneMode }>();
const requestedSession = ref<string | null>(null);
const memoryKey = () => `htd.cache-chart.${props.harness}`;
function savedChart() { try { return localStorage.getItem(memoryKey()); } catch { return null; } }
const chartSession = ref<string | null>(savedChart());
watch(chartSession, value => { try { if (value) localStorage.setItem(memoryKey(), value); else localStorage.removeItem(memoryKey()); } catch { /* Optional preference. */ } });
const cursor = ref<string | null>(null);
const query = useCacheMonitor(() => props.harness, () => props.timezone, requestedSession, cursor, Date.now, chartSession);
type Session = CommandData<'stats.cacheMonitor'>['sessionHitRates'][number];
const directory = shallowRef<Session[]>([]);
watch(query.data, data => {
  if (!data) return;
  // Pin the first loaded chart too; new sessions cannot replace a valid selection.
  if (data.chartSessionRef !== chartSession.value) chartSession.value = data.chartSessionRef;
  const cutoff = Date.parse(data.nextRefreshAt) - 60_000 - 20 * 60_000;
  const values = new Map((cursor.value ? directory.value : []).filter(row => Date.parse(row.lastActivityAt) > cutoff).map(row => [row.sessionRef!, row]));
  data.sessionHitRates.forEach(row => values.set(row.sessionRef!, row));
  directory.value = [...values.values()].slice(-500);
});
watch(() => props.harness, () => { requestedSession.value = null; chartSession.value = savedChart(); cursor.value = null; directory.value = []; }, { flush: 'sync' });
const sessionRates = computed(() => [...directory.value].sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.sessionKey.localeCompare(b.sessionKey)));
const selectedSession = computed(() => sessionRates.value.find(value => value.sessionRef === query.data.value?.selectedSessionRef));
const points = computed<ChartPoint[]>(() => query.data.value?.trend.map(point => ({ label: point.label, value: point.cacheHitRate, flags: { cacheBust: point.cacheBust, cacheLoss: point.cacheLoss, contextCompacted: point.contextCompacted } })) ?? []);
const hitRate = (value: number | null | undefined) => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const state = (value: Session) => value.frozen ? '已冻结' : '进行中';
const number = (value: Session) => sessionRates.value.findIndex(row => row.sessionRef === value.sessionRef) + 1;
const sessionOptions = () => sessionRates.value.map((value, index) => ({ value: value.sessionRef!, label: `会话${index + 1}  ${value.modelName}  ${formatDateTime(value.startedAt, props.timezone)}\n会话缓存命中率 ${hitRate(value.cacheHitRate)}  会话平均tps ${value.sessionAverageTps ?? '—'} t/s  ${state(value)}` })).reverse();
const chartOptions = () => sessionRates.value.map((value, index) => ({value:value.sessionRef!,label:`会话${index + 1}  ${formatDateTime(value.startedAt, props.timezone)}\n${value.modelName}  ${state(value)}`})).reverse();
const chartLabel = computed(() => {
  const value = sessionRates.value.find(row => row.sessionRef === chartSession.value);
  return `柱状图视图：${value ? `会话${number(value)}` : '无会话'}`;
});
const sessionLabel = computed(() => {
  const value = selectedSession.value;
  return value ? `会话${number(value)}  ${value.modelName}  命中率 ${hitRate(value.cacheHitRate)}  tps ${value.tokensPerSecond ?? value.sessionAverageTps ?? '—'} t/s  ${state(value)}` : '暂无会话数据';
});
function selectSession(value: string) { requestedSession.value = value; cursor.value = null; }
function more() { requestedSession.value = query.data.value?.selectedSessionRef ?? null; cursor.value = query.data.value?.nextCursor ?? null; }
defineExpose({ refresh: query.refresh });
</script>
<template>
  <section class="monitor-panel" aria-label="近期缓存监控" :aria-busy="query.loading.value || query.refreshing.value">
    <header><h2>近期缓存监控</h2><div class="monitor-tools">
      <div class="session-info" aria-label="会话信息"><ChoiceMenu :key="harness" :model-value="query.data.value?.selectedSessionRef ?? ''" label="监控会话" :show-label="false" :selected-label="sessionLabel" :options="sessionOptions" @update:model-value="selectSession" /></div>
      <div class="chart-tools"><Button v-if="query.data.value?.nextCursor" :disabled="query.refreshing.value" @click="more">更多会话</Button><ChoiceMenu :model-value="chartSession ?? ''" label="柱状图视图" :show-label="false" :selected-label="chartLabel" :options="chartOptions" @update:model-value="chartSession = $event || null" /></div>
    </div></header>
    <p class="text-muted monitor-note">缓存命中率 · 每分钟更新，统计至上一完整分钟</p>
    <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
    <Skeleton v-if="query.loading.value" height="240px" />
    <BarChart v-else :points="points" label="近期缓存命中率" y-format="percent" :x-tick-count="5" warning-grain="minute" show-warnings :height="214" empty-message="" />
    <div class="cache-legend" aria-label="缓存警示图例"><p><i class="bust-swatch" aria-hidden="true" /><span><strong>缓存击穿 / 上下文压缩</strong>上下文 ≥ 5 万且命中率低于 10%时提示击穿；已识别的压缩单独说明，悬停查看原因</span></p><p><i class="loss-swatch" aria-hidden="true" /><span><strong>大量未命中</strong>单次交互未命中输入 ≥ 5 万；击穿时不重复显示</span></p></div>
  </section>
</template>
<style scoped>
.monitor-panel{padding-block:24px 8px;border-top:1px solid var(--border)}header{display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap;gap:12px}h2{font-size:18px}.monitor-tools{display:flex;align-items:center;justify-content:flex-end;gap:8px;max-width:100%;min-width:0}.session-info{max-width:100%;min-width:0;flex:0 1 auto;font-size:12px;font-variant-numeric:tabular-nums}.session-info :deep(.choice-trigger){max-width:100%;padding-inline:10px;gap:8px}.session-info :deep(.choice-value){white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.session-info :deep(.choice-trigger>svg){flex-shrink:0}.session-info :deep(.choice-options){left:auto;right:0;width:max-content;max-width:min(560px,calc(100vw - 80px));min-width:100%;max-height:280px;overflow:auto}.session-info :deep(.choice-options button span){white-space:pre-line;line-height:1.8;font-size:12px}.chart-tools{display:flex;align-items:center;gap:8px;flex-shrink:0}.chart-tools :deep(.choice-trigger){min-width:0;font-size:12px}.chart-tools :deep(.choice-options){left:auto;right:0;width:max-content;max-width:320px;max-height:260px;overflow:auto}.monitor-note{margin-block:10px 16px;font-size:13px}.cache-legend{display:flex;flex-wrap:wrap;gap:12px 28px;padding-top:12px;font-size:13px;color:var(--text-muted)}.cache-legend p{display:flex;align-items:start;gap:10px}.cache-legend strong{display:block;color:var(--text);font-weight:500;margin-bottom:2px}.cache-legend i{width:16px;height:8px;margin-top:6px;border-radius:2px;flex-shrink:0}.bust-swatch{background:linear-gradient(180deg,#ff6680,#d31342 55%,#9a0b32);box-shadow:0 0 7px var(--cache-bust-glow)}.loss-swatch{background:linear-gradient(180deg,#b8325c,#7d0f38 60%,#4d0621);box-shadow:0 0 7px var(--cache-loss-glow)}@media(max-width:900px){.monitor-tools{justify-content:flex-start;width:100%}.session-info :deep(.choice-options){right:auto;left:0;min-width:0}}
</style>
<style scoped>.chart-tools :deep(.choice-options button span){white-space:pre-line;line-height:1.7}</style>
