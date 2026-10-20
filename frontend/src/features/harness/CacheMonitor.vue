<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useCacheMonitor } from './useCacheMonitor';
import type { Harness } from './types';
import type { TimeZoneMode } from '../../shared/time';
import { formatDateTime } from '../../shared/format';
import SegmentedControl from '../../ui/SegmentedControl.vue';
import BarChart from '../../ui/charts/BarChart.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import EmptyState from '../../ui/EmptyState.vue';
import Skeleton from '../../ui/Skeleton.vue';
import type { ChartPoint } from '../../ui/charts/axis';
const props = defineProps<{ harness: Harness; mode: 'minute' | 'hour'; timezone: TimeZoneMode }>();
const emit = defineEmits<{ 'update:mode': [value: 'minute' | 'hour'] }>();
const query = useCacheMonitor(() => props.harness, () => props.mode, () => props.timezone);
const points = computed<ChartPoint[]>(() => query.data.value?.trend.map(point => ({ label: point.label, value: point.cacheHitRate, flags: { cacheBust: point.cacheBust, cacheLoss: point.cacheLoss, contextCompacted: point.contextCompacted } })) ?? []);
const speeds = computed(() => query.data.value?.outputSpeeds ?? []);
const activeSpeeds = computed(() => speeds.value.filter(value => !value.frozen));
const speed = computed(() => activeSpeeds.value[0] ?? speeds.value[0]);
const expanded = ref(false);
const sessionRates = computed(() => query.data.value?.sessionHitRates ?? []);
const selectedSessionKey = ref('');
const sessionMenu = ref<HTMLDetailsElement>();
const selectedSession = computed(() => sessionRates.value.find(value => value.sessionKey === selectedSessionKey.value)
  ?? sessionRates.value.find(value => !value.frozen) ?? sessionRates.value[0]);
watch(sessionRates, values => {
  const current = values.find(value => value.sessionKey === selectedSessionKey.value);
  const active = values.find(value => !value.frozen);
  if (!current || (current.frozen && active)) selectedSessionKey.value = active?.sessionKey ?? values[0]?.sessionKey ?? '';
});
watch(() => props.harness, () => { expanded.value = false; selectedSessionKey.value = ''; });
function hitRate(value: number | null | undefined) { return value == null ? '—' : `${(value * 100).toFixed(1)}%`; }
function sessionNote(value: typeof selectedSession.value) {
  if (!value) return '暂无会话数据';
  return `${value.cacheCoverageIncomplete ? '部分请求缓存数据未知；' : ''}${value.frozen ? '会话已冻结' : '会话进行中'}`;
}
function selectSession(key: string) { selectedSessionKey.value = key; if (sessionMenu.value) sessionMenu.value.open = false; }
const speedNote = (value: typeof speed.value) => value?.frozen ? '冻结中' : '监测中';
defineExpose({ refresh: query.refresh });
</script>

<template>
  <section class="monitor-panel" aria-label="近期缓存监控" :aria-busy="query.loading.value || query.refreshing.value">
    <header>
      <h2>近期缓存监控</h2>
      <div class="monitor-tools">
        <details v-if="activeSpeeds.length > 1" class="output-speed multi-speed" aria-label="近60分钟模型输出速度" :open="expanded" @toggle="expanded = ($event.target as HTMLDetailsElement).open">
          <summary aria-label="展开所有运行模型的TPS">TPS · {{ activeSpeeds.length }} 模型<span aria-hidden="true">⌄</span></summary>
          <div v-if="expanded" class="speed-list surface" aria-label="正在运行的模型TPS"><p v-for="value in activeSpeeds" :key="value.modelKey" :title="speedNote(value)"><span>{{ value.displayName }}</span><strong>{{ value.tokensPerSecond ?? '—' }} t/s</strong></p></div>
        </details>
        <div v-else class="output-speed" aria-label="近60分钟模型输出速度" :title="speedNote(speed)"><strong>{{ speed ? `${speed.displayName} tps：${speed.tokensPerSecond ?? '—'} t/s` : '模型 tps：— t/s' }}</strong></div>
        <details v-if="sessionRates.length > 1" ref="sessionMenu" class="output-speed session-rate multi-session" aria-label="当前会话缓存命中率">
          <summary :title="sessionNote(selectedSession)">当前会话缓存命中率：{{ hitRate(selectedSession?.cacheHitRate) }} · {{ sessionRates.length }} 会话<span aria-hidden="true">⌄</span></summary>
          <div class="session-list surface" aria-label="会话缓存命中率列表"><button v-for="value in sessionRates" :key="value.sessionKey" type="button" :aria-pressed="selectedSession?.sessionKey === value.sessionKey" :title="sessionNote(value)" @click="selectSession(value.sessionKey)"><span>{{ value.displayName }} · {{ formatDateTime(value.startedAt, timezone) }} · {{ value.sessionKey.slice(-4) }}{{ value.frozen ? ' · 已冻结' : '' }}</span><strong>{{ hitRate(value.cacheHitRate) }}</strong></button></div>
        </details>
        <div v-else class="output-speed session-rate" aria-label="当前会话缓存命中率" :title="sessionNote(selectedSession)"><strong>当前会话缓存命中率：{{ hitRate(selectedSession?.cacheHitRate) }}</strong></div>
      </div>
      <SegmentedControl :model-value="mode" label="缓存监控范围" :options="[{ value: 'minute', label: '最近 60 分钟' }, { value: 'hour', label: '最近 24 小时' }]" @update:model-value="emit('update:mode', $event)" />
    </header>
    <p class="text-muted monitor-note">缓存命中率 · 每分钟更新，统计至上一完整分钟，按已探测缓存的输入加权</p>
    <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
    <Skeleton v-if="query.loading.value" height="240px" />
    <template v-else>
      <EmptyState v-if="!points.length && !query.error.value" :message="mode === 'minute' ? '最近 60 分钟暂无缓存数据' : '最近 24 小时暂无缓存数据'" />
      <BarChart :points="points" label="近期缓存命中率" y-format="percent" :x-tick-count="5" :warning-grain="query.data.value?.grain ?? mode" show-warnings :height="214" empty-message="" />
    </template>
    <div class="cache-legend" aria-label="缓存警示图例">
      <p><i class="bust-swatch" aria-hidden="true" /><span><strong>缓存击穿 / 上下文压缩</strong>上下文 ≥ 5 万且命中率低于 10%时提示击穿；已识别的压缩单独说明，悬停查看原因</span></p>
      <p><i class="loss-swatch" aria-hidden="true" /><span><strong>大量未命中</strong>单次交互未命中输入 ≥ 5 万；击穿时不重复显示</span></p>
    </div>
  </section>
</template>

<style scoped>
.monitor-panel { padding-block: 28px 8px; border-top: 1px solid var(--border); }header { display: grid; grid-template-columns:auto minmax(0,1fr) auto; align-items: center; gap: 12px; }.monitor-tools{display:flex;align-items:center;justify-self:end;gap:8px;min-width:0;max-width:100%}
.monitor-note { margin-block: 10px 16px; font-size: 13px; }
.output-speed { display:flex;align-items:center;justify-self:stretch;min-width:0;max-width:100%;height:36px;padding:0 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);font-size:12px;color:var(--money);font-variant-numeric:tabular-nums }.output-speed>strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}.multi-speed{position:relative;padding:0}.multi-speed summary{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;height:100%;min-height:34px;box-sizing:border-box;padding:0 10px;cursor:pointer;list-style:none;white-space:nowrap}.multi-speed summary::-webkit-details-marker{display:none}.speed-list,.session-list{position:absolute;z-index:4;top:calc(100% + 6px);left:0;width:max-content;max-width:min(360px,80vw);max-height:260px;overflow:auto;padding:8px 12px;border:1px solid var(--border);border-radius:6px;box-shadow:0 8px 20px rgb(0 0 0 / 14%)}.speed-list p,.session-list button{display:flex;align-items:baseline;justify-content:space-between;gap:24px;width:100%;padding-block:8px}.speed-list p span,.session-list button span{color:var(--text);overflow-wrap:anywhere}.speed-list strong,.session-list button strong{white-space:nowrap}.session-list button{border:0;background:transparent;text-align:left;color:var(--money)}.session-list button:hover,.session-list button[aria-pressed='true']{background:var(--surface-2)}.multi-session .session-list{left:auto;right:0}
@media(max-width:760px){header{grid-template-columns:auto minmax(0,1fr)}.monitor-tools{grid-column:1 / -1;justify-self:start;width:100%;flex-wrap:wrap}header>:last-child{grid-column:1 / -1;justify-self:end}}
header h2 { font-size: 18px; }
.cache-legend { display: flex; flex-wrap: wrap; gap: 12px 28px; padding-top: 12px; font-size: 13px; color: var(--text-muted); }
.cache-legend p { display: flex; align-items: start; gap: 10px; }
.cache-legend strong { display: block; color: var(--text); font-weight: 500; margin-bottom: 2px; }
.cache-legend i { width: 16px; height: 8px; margin-top: 6px; border-radius: 2px; flex-shrink: 0; }
.bust-swatch { background: linear-gradient(180deg, #ff6680 0%, #d31342 55%, #9a0b32 100%); box-shadow: 0 0 7px var(--cache-bust-glow); }
.loss-swatch { background: linear-gradient(180deg, #b8325c 0%, #7d0f38 60%, #4d0621 100%); box-shadow: 0 0 7px var(--cache-loss-glow); }
</style>
