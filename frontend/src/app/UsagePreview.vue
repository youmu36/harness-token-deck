<script setup lang="ts">
import { inject, ref, watchEffect } from 'vue';
import { BridgeKey, type CommandData } from './ports';
import { unwrap } from './errors';
import { useQuery } from '../shared/query';
import { useShellPreferences } from './useShellPreferences';
import { useTimeRange } from '../features/usage-common/useTimeRange';
import TimeRangeBar from '../features/usage-common/TimeRangeBar.vue';
import CollectionStatus from '../features/usage-common/CollectionStatus.vue';
import ScanButton from '../features/usage-common/ScanButton.vue';
import MetricsGrid from '../features/usage-common/MetricsGrid.vue';
import UsageTrend from '../features/usage-common/UsageTrend.vue';
import ModelTable from '../features/usage-common/ModelTable.vue';
import HourlyTable from '../features/usage-common/HourlyTable.vue';
import SegmentedControl from '../ui/SegmentedControl.vue';
import InlineAlert from '../ui/InlineAlert.vue';
import type { RangePreset, UsageFilter, UsageRange } from '../features/usage-common/types';
const props = defineProps<{ initialPreferences: CommandData<'preferences.get'> }>();
const bridge = inject(BridgeKey)!;
const time = useTimeRange('codex', props.initialPreferences);
const preferences = useShellPreferences(props.initialPreferences);
const theme = ref(new URLSearchParams(location.search).get('theme') === 'dark' ? 'dark' : 'light');
watchEffect(() => { document.documentElement.dataset.theme = theme.value; });
const status = useQuery('preview.collection', async () => unwrap(await bridge.call('collection.status', {})), { events: ['sources.changed', 'collection.tick'] });
const summary = useQuery('preview.usage', async () => {
  const range = time.advance();
  return unwrap(await bridge.call('stats.summary', { harness: 'codex', start: range.start, end: range.end, offsetMinutes: range.offsetMinutes, grain: time.filter.value.grain }));
}, { deps: [() => [time.filter.value.range, time.filter.value.start, time.filter.value.end, time.filter.value.grain, time.mode.value]],
  events: [{ name: 'usage.changed', when: event => event.harnesses.includes('codex') }] });
async function select(value: RangePreset) { try { await time.select(value); } catch { /* Persistence error below. */ } }
async function apply(value: UsageRange) { try { await time.applyCustom(value); } catch { /* Persistence error below. */ } }
async function filter(patch: Partial<UsageFilter>) { try { await time.saveFilter(patch); } catch { /* Persistence error below. */ } }
async function setTimezone(value: 'system' | 'UTC') { try { await preferences.save({ timezone: value }); } catch { /* Persistence error below. */ } }
</script>

<template>
  <main class="content usage-preview">
    <header class="preview-heading"><div><p class="text-muted">浏览器预览 · 示例数据</p><h1>用量组件</h1></div>
      <div class="preview-controls"><SegmentedControl v-model="theme" label="预览主题" :options="[{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }]" />
        <SegmentedControl :model-value="time.mode.value" label="预览时区" :options="[{ value: 'system', label: '系统时区' }, { value: 'UTC', label: 'UTC' }]" @update:model-value="setTimezone" /></div>
    </header>
    <TimeRangeBar :model-value="time.selection.value" :range="time.range.value" :mode="time.mode.value" :custom-start="time.filter.value.start" :custom-end="time.filter.value.end" @update:model-value="select" @apply="apply">
      <div class="preview-controls"><CollectionStatus harness="codex" :status="status.data.value" :error="status.error.value?.message" :mode="time.mode.value" />
        <ScanButton show-countdown :next-periodic-at="status.data.value?.nextPeriodicAt" :paused="status.data.value?.paused" @completed="summary.refresh()" /></div>
    </TimeRangeBar>
    <InlineAlert v-if="time.error.value || preferences.error.value" severity="danger" retry @retry="time.retry(); preferences.retry()">{{ time.error.value || preferences.error.value }}</InlineAlert>
    <InlineAlert v-if="summary.error.value" severity="danger" retry @retry="summary.refresh">{{ summary.error.value.message }}</InlineAlert>
    <MetricsGrid :metrics="summary.data.value?.metrics" harness="codex" :loading="summary.loading.value" />
    <UsageTrend :summary="summary.data.value" :start="time.range.value.start" :end="time.range.value.end" :metric="time.filter.value.metric" :grain="time.filter.value.grain" @update:metric="filter({ metric: $event })" @update:grain="filter({ grain: $event })" />
    <ModelTable :models="summary.data.value?.models ?? []" />
    <HourlyTable :models="summary.data.value?.models ?? []" :hourly="summary.data.value?.hourly ?? []" :hourly-totals="summary.data.value?.hourlyTotals ?? []" :model-value="time.filter.value.modelKey" :loading="summary.loading.value" @update:model-value="filter({ modelKey: $event })" />
  </main>
</template>

<style scoped>
.usage-preview { max-width: 1440px; display: grid; gap: 24px; padding-block: 28px; }
.preview-heading, .preview-controls { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
h1 { margin-top: 6px; }
</style>
