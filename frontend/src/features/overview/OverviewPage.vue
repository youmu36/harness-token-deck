<script setup lang="ts">
import { computed, inject, ref } from 'vue';
import Button from '../../ui/Button.vue';
import { UsageNavigationKey } from '../../app/usageNavigation';
import { BridgeKey, type CommandData } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useShellPreferences } from '../../app/useShellPreferences';
import { useQuery } from '../../shared/query';
import { useTimeRange } from '../usage-common/useTimeRange';
import type { RangePreset, UsageFilter, UsageRange } from '../usage-common/types';
import TimeRangeBar from '../usage-common/TimeRangeBar.vue';
import UsageHeader from '../usage-common/UsageHeader.vue';
import MetricsGrid from '../usage-common/MetricsGrid.vue';
import UnpricedNotice from '../usage-common/UnpricedNotice.vue';
import SectionNav from '../../ui/SectionNav.vue';
import AnalyticsPanel from './AnalyticsPanel.vue';
import AnalysisPage from './AnalysisPage.vue';
import UsageTrend from '../usage-common/UsageTrend.vue';
import ModelTable from '../usage-common/ModelTable.vue';
import HourlyDetails from '../usage-common/HourlyDetails.vue';
import HarnessSummaryTable from './HarnessSummaryTable.vue';
import ReconciliationNotice from '../reconciliation/ReconciliationNotice.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import type { UsageHarness } from '../usage-common/types';
const props = defineProps<{ initialPreferences: CommandData<'preferences.get'>; analysisFilter?: Partial<UsageFilter>; canReturn?: boolean }>();
const bridge = inject(BridgeKey)!;
const time = useTimeRange('overview', props.initialPreferences, () => new Date(), props.analysisFilter);
const selectedDay = ref('');
const navigation = inject(UsageNavigationKey, undefined);
const preferences = useShellPreferences(props.initialPreferences);
const section = ref('statistics');
const period = ref('recent');
const recent = computed(() => section.value === 'statistics' && period.value === 'recent');
const status = useQuery('overview.collection', async () => unwrap(await bridge.call('collection.status', { details: false })), { events: ['sources.changed', 'collection.tick'] });
const summary = useQuery('overview.summary', async () => {
  const range = time.advance();
  return unwrap(await bridge.call('stats.summary', { harness: time.filter.value.harness, start: range.start, end: range.end, offsetMinutes: range.offsetMinutes, grain: time.queryGrain.value, sections: ['trend', 'models', 'harnesses'] }));
}, { visible: recent, deps: [time.queryKey], events: [{ name: 'usage.changed', when: event => time.filter.value.harness === 'all' || event.harnesses.includes(time.filter.value.harness) }], clearOnChange: true, coalesce: true });
let scanCheckpoint = summary.checkpoint();
function scanning() { scanCheckpoint = summary.checkpoint(); }
function scanned() { void summary.refreshIfUnchanged(scanCheckpoint); }
async function select(value: RangePreset) { try { await time.select(value); } catch { /* The page retains the error and draft. */ } }
async function apply(value: UsageRange) { try { await time.applyCustom(value); } catch { /* The page retains the error and draft. */ } }
async function filter(patch: Partial<UsageFilter>) { try { await time.saveFilter(patch); } catch { /* The page retains the error and draft. */ } }
</script>

<template>
  <div class="overview-page">
    <Button v-if="canReturn" class="return-link" @click="navigation?.back()">返回 Harness 统计</Button>
    <UsageHeader title="数据统计与分析" harness="all" :status="status.data.value" :error="status.error.value?.message" :mode="time.mode.value" :countdown="preferences.snapshot.value.preferences.showScanCountdown" @scanning="scanning" @scanned="scanned" />
    <SectionNav v-model="section" label="统计分析子页" :items="[{value:'statistics',label:'数据统计'}, {value:'analysis',label:'数据分析'}]" />
    <AnalysisPage v-if="section === 'analysis'" :harness="time.filter.value.harness" :range="time.range.value" :filter="time.filter.value" :mode="time.mode.value" @update:harness="filter({harness:$event,modelKey:null})" />
    <template v-else>
    <p class="text-muted page-purpose">按来源保存和查看 Token 用量、缓存命中率与本地估算成本。</p>
    <ChoiceMenu v-model="period" label="分析周期" :options="[{value:'recent',label:'近期范围'}, {value:'month',label:'本月'}, {value:'quarter',label:'季度'}, {value:'year',label:'年份'}, {value:'years',label:'多年份'}, {value:'all',label:'全部历史'}]" />
    <AnalyticsPanel v-if="!recent" :period="period as 'month' | 'quarter' | 'year' | 'years' | 'all'" :models="false" statistics :harness="time.filter.value.harness" :range="time.range.value" :filter="time.filter.value" :mode="time.mode.value" @update:harness="filter({harness:$event,modelKey:null})" />
    <template v-if="recent">
    <ReconciliationNotice :harness="time.filter.value.harness === 'all' ? undefined : time.filter.value.harness" />
    <TimeRangeBar :model-value="time.selection.value" :range="time.range.value" :mode="time.mode.value" :custom-start="time.filter.value.start" :custom-end="time.filter.value.end" @update:model-value="select" @apply="apply">
      <template #source><ChoiceMenu :model-value="time.filter.value.harness" label="数据来源" :options="[
        { value: 'all', label: '全部' }, { value: 'claude', label: 'Claude Code' }, { value: 'codex', label: 'Codex' },
        { value: 'zcode', label: 'Zcode' }, { value: 'dsh', label: 'Deepseek Harness' },
      ]" @update:model-value="filter({ harness: $event as UsageHarness, modelKey: null })" /></template>
    </TimeRangeBar>
    <InlineAlert v-if="time.error.value" severity="danger" retry @retry="time.retry">{{ time.error.value }}</InlineAlert>
    <InlineAlert v-if="summary.error.value" severity="danger" retry @retry="summary.refresh">{{ summary.error.value.message }}</InlineAlert>
    <UnpricedNotice :initial-preferences="initialPreferences" />
    <MetricsGrid :metrics="summary.data.value?.metrics" :harness="time.filter.value.harness" :loading="summary.loading.value" />
    <InlineAlert v-if="summary.data.value?.historyNotice">{{ summary.data.value.historyNotice }}</InlineAlert>
    <HarnessSummaryTable v-if="time.filter.value.harness === 'all'" :harnesses="summary.data.value?.harnesses ?? []" />
    <UsageTrend :summary="summary.data.value" :start="time.range.value.start" :end="time.range.value.end" :metric="time.filter.value.metric" :grain="time.trendGrain.value" :daily-only="time.dailyOnly.value" @update:metric="filter({ metric: $event })" @update:grain="filter({ grain: $event })" @select-day="selectedDay = $event" />
    <ModelTable :models="summary.data.value?.models ?? []" />
    <HourlyDetails :focus-bucket="selectedDay" :harness="time.filter.value.harness" :range="time.range.value" :query-key="time.queryKey.value" :models="summary.data.value?.models ?? []" :model-value="time.filter.value.modelKey" @update:model-value="filter({ modelKey: $event })" />
    </template>
    </template>
  </div>
</template>

<style scoped>
.supplier-consumption{padding:18px;display:grid;gap:10px}.supplier-consumption p,.supplier-consumption div{font-size:13px;}
.return-link { justify-self: start; }.overview-page { display: grid; gap: 24px; }.overview-page > * { min-width: 0; }.collection-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
</style>
