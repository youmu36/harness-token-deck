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
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import type { ChartPoint } from '../../ui/charts/axis';
const props = defineProps<{ harness: Harness; mode: 'minute' | 'hour'; timezone: TimeZoneMode }>();
const emit = defineEmits<{ 'update:mode': [value: 'minute' | 'hour'] }>();
const query = useCacheMonitor(() => props.harness, () => props.mode, () => props.timezone);
const points = computed<ChartPoint[]>(() => query.data.value?.trend.map(point => ({ label: point.label, value: point.cacheHitRate, flags: { cacheBust: point.cacheBust, cacheLoss: point.cacheLoss, contextCompacted: point.contextCompacted } })) ?? []);
const speeds = computed(() => query.data.value?.outputSpeeds ?? []);
const sessionRates = computed(() => {
  const values = [...(query.data.value?.sessionHitRates ?? [])].sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.sessionKey.localeCompare(b.sessionKey));
  const active = values.filter(value => !value.frozen);
  return active.length === 1 ? active : values;
});
const selectedSessionKey = ref('');
const selectedSession = computed(() => sessionRates.value.find(value => value.sessionKey === selectedSessionKey.value)
  ?? sessionRates.value.find(value => !value.frozen) ?? sessionRates.value[0]);
watch(sessionRates, values => {
  const current = values.find(value => value.sessionKey === selectedSessionKey.value);
  const active = values.find(value => !value.frozen);
  if (!current) selectedSessionKey.value = active?.sessionKey ?? values[0]?.sessionKey ?? '';
});
watch(() => props.harness, () => { selectedSessionKey.value = ''; selectedSpeedKey.value = ''; });
function hitRate(value: number | null | undefined) { return value == null ? '—' : `${(value * 100).toFixed(1)}%`; }
const shortStart = (value: string) => formatDateTime(value, props.timezone).replace(/\//g, '-').replace('T', ' ').slice(5, 16);
const state = (value: { frozen: boolean }) => value.frozen ? '已冻结' : '监测中';
const sessionOptions = computed(() => sessionRates.value.map((value, index) => ({ value: value.sessionKey,
  label: `会话${sessionNumbers.value.get(value.sessionKey) ?? index + 1}  ${value.displayName}  ${shortStart(value.startedAt)}\n当前会话缓存命中率：${hitRate(value.cacheHitRate)}  ${state(value)}${value.cacheCoverageIncomplete ? '（覆盖不完整）' : ''}`,
})));
const sessionLabel = computed(() => selectedSession.value
  ? `会话${sessionNumbers.value.get(selectedSession.value.sessionKey) ?? '—'}  缓存命中 ${hitRate(selectedSession.value.cacheHitRate)}  ${state(selectedSession.value)}`
  : '暂无会话数据');
const sessionTooltip = '本数值表示当前会话缓存命中率，\n10分钟无新增输入视为会话结束，\n将冻结本次数据';
const sessionNumbers = computed(() => {
  const starts = new Map<string, string>();
  for (const row of [...sessionRates.value, ...speeds.value]) {
    if (!row.sessionKey || !row.startedAt) continue;
    starts.set(row.sessionKey, [starts.get(row.sessionKey) ?? row.startedAt, row.startedAt].sort()[0]!);
  }
  return new Map([...starts].sort((a, b) => a[1].localeCompare(b[1]) || a[0].localeCompare(b[0])).map(([key], index) => [key, index + 1]));
});
const speedKey = (value: typeof speeds.value[number]) => `${value.sessionKey ?? 'legacy'}:${value.modelKey}`;
const selectedSpeedKey = ref('');
const speed = computed(() => speeds.value.find(value => speedKey(value) === selectedSpeedKey.value)
  ?? speeds.value.find(value => !value.frozen) ?? speeds.value[0]);
const speedSession = (value: typeof speeds.value[number]) => value.sessionKey ? `会话${sessionNumbers.value.get(value.sessionKey) ?? '—'}` : '会话未知';
const speedOptions = computed(() => speeds.value.map(value => ({ value: speedKey(value),
  label: `${speedSession(value)}  ${value.displayName}  ${value.startedAt ? shortStart(value.startedAt) : '开始时间未知'}  ${state(value)}\n近似实时tps ${value.tokensPerSecond ?? '—'} t/s  会话平均tps ${value.sessionAverageTps ?? '—'} t/s`,
})));
const speedLabel = computed(() => speed.value
  ? `${speedSession(speed.value)}  近似实时tps ${speed.value.tokensPerSecond ?? '—'} t/s  ${state(speed.value)}` : '暂无会话tps');
const speedTooltip = '近似实时tps取最近三个有效输出分钟，\n仅供参考，10分钟无新增输入视为会话结束，\n将冻结本次数据';

defineExpose({ refresh: query.refresh });
</script>

<template>
  <section class="monitor-panel" aria-label="近期缓存监控" :aria-busy="query.loading.value || query.refreshing.value">
    <header>
      <h2>近期缓存监控</h2>
      <div class="monitor-tools">
        <div class="output-speed" aria-label="会话输出速度" >
          <span class="monitor-help" role="tooltip">{{ speedTooltip }}</span>
          <ChoiceMenu :key="harness" :model-value="speed ? speedKey(speed) : ''" label="会话输出速度" :show-label="false" :selected-label="speedLabel" placeholder="暂无会话tps" :options="speedOptions" @update:model-value="selectedSpeedKey = $event" />
        </div>
        <div class="session-rate" aria-label="当前会话缓存命中率" >
          <span class="monitor-help" role="tooltip">{{ sessionTooltip }}</span>
          <ChoiceMenu :key="harness" :model-value="selectedSession?.sessionKey ?? ''" label="当前会话缓存命中率" :show-label="false" :selected-label="sessionLabel" placeholder="暂无会话数据" :options="sessionOptions" @update:model-value="selectedSessionKey = $event" />
        </div>
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
.session-rate{position:relative;width:max-content;max-width:100%;min-width:0;flex:0 1 auto;font-size:12px;color:var(--money);font-variant-numeric:tabular-nums;z-index:20}.session-rate :deep(.choice-trigger){width:100%;min-width:0;padding-inline:8px;gap:6px}.session-rate :deep(.choice-label){flex-shrink:0}.session-rate :deep(.choice-value){overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-inline-end:1.5ch}.session-rate :deep(.choice-trigger>svg){flex-shrink:0;margin-left:auto}.session-rate :deep(.choice-options){left:auto;right:0;width:min(430px,calc(100vw - 48px));min-width:min(272px,calc(100vw - 48px));max-height:260px;overflow:auto}.session-rate :deep(.choice-options button span){white-space:pre-line;line-height:1.7}.session-rate :deep(.choice-options button){font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.output-speed{position:relative;min-width:0;max-width:100%;width:max-content;flex:0 1 auto;font-size:12px;color:var(--money);font-variant-numeric:tabular-nums;z-index:21}.output-speed :deep(.choice-trigger){width:100%;min-width:0;height:36px;padding-inline:8px;gap:6px}.output-speed :deep(.choice-value){overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-inline-end:1.5ch}.output-speed :deep(.choice-trigger>svg){flex-shrink:0;margin-left:auto}.output-speed :deep(.choice-options){width:min(480px,calc(100vw - 48px));min-width:100%;max-height:280px;overflow:auto}.output-speed :deep(.choice-options button span){white-space:pre-line;overflow-wrap:anywhere;line-height:1.7}
@media(max-width:1150px){header{grid-template-columns:auto minmax(0,1fr)}.monitor-tools{grid-column:1 / -1;grid-row:2;justify-self:start;width:100%;flex-wrap:wrap}header>:last-child{grid-column:2;grid-row:1;justify-self:end}}
@media(max-width:760px){header{grid-template-columns:auto minmax(0,1fr)}.monitor-tools{grid-column:1 / -1;justify-self:start;width:100%;flex-wrap:wrap}header>:last-child{grid-column:1 / -1;grid-row:3;justify-self:end}}
@media(max-width:760px){.session-rate :deep(.choice-options){left:0;right:auto;width:min(430px,calc(100vw - 110px));min-width:100%}}
header h2 { font-size: 18px; }
.cache-legend { display: flex; flex-wrap: wrap; gap: 12px 28px; padding-top: 12px; font-size: 13px; color: var(--text-muted); }
.cache-legend p { display: flex; align-items: start; gap: 10px; }
.cache-legend strong { display: block; color: var(--text); font-weight: 500; margin-bottom: 2px; }
.cache-legend i { width: 16px; height: 8px; margin-top: 6px; border-radius: 2px; flex-shrink: 0; }
.bust-swatch { background: linear-gradient(180deg, #ff6680 0%, #d31342 55%, #9a0b32 100%); box-shadow: 0 0 7px var(--cache-bust-glow); }
.loss-swatch { background: linear-gradient(180deg, #b8325c 0%, #7d0f38 60%, #4d0621 100%); box-shadow: 0 0 7px var(--cache-loss-glow); }
.monitor-help{display:none;position:absolute;bottom:calc(100% + 8px);right:0;width:max-content;max-width:min(320px,80vw);padding:10px 12px;background:var(--surface);border:1px solid var(--border);border-radius:6px;white-space:pre-line;font-size:12px;line-height:1.7;color:var(--text);box-shadow:0 6px 20px #0003;pointer-events:none;z-index:30}.output-speed:hover .monitor-help,.output-speed:focus-within .monitor-help,.session-rate:hover .monitor-help,.session-rate:focus-within .monitor-help{display:block}
</style>
