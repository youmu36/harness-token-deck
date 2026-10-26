<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { UsageNavigationKey } from '../../app/usageNavigation';
import { BridgeKey, type CommandData } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { readShared } from '../../shared/readService';
import { useTimeRange } from '../usage-common/useTimeRange';
import { useShellPreferences } from '../../app/useShellPreferences';
import { harnessNames, type Harness, type PricingSelection } from './types';
import TimeRangeBar from '../usage-common/TimeRangeBar.vue';
import UsageHeader from '../usage-common/UsageHeader.vue';
import MetricsGrid from '../usage-common/MetricsGrid.vue';
import UnpricedNotice from '../usage-common/UnpricedNotice.vue';
import ModelTable from '../usage-common/ModelTable.vue';
import HourlyDetails from '../usage-common/HourlyDetails.vue';
import CacheMonitor from './CacheMonitor.vue';
import CurrentPricingButton from './CurrentPricingButton.vue';
import VendorBalanceCard from './VendorBalanceCard.vue';
import ReconciliationNotice from '../reconciliation/ReconciliationNotice.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Button from '../../ui/Button.vue';
import RecalcDialog from '../recalc/RecalcDialog.vue';
import type { RangePreset, UsageFilter, UsageRange } from '../usage-common/types';
const props = defineProps<{ harness: Harness; initialPreferences: CommandData<'preferences.get'> }>();
const emit = defineEmits<{ ready: [] }>();
const bridge = inject(BridgeKey)!
const time = useTimeRange(props.harness, props.initialPreferences);
const navigation = inject(UsageNavigationKey);
const hourlyAllowed = computed(() => Date.parse(time.range.value.end) - Date.parse(time.range.value.start) <= 7 * 86400000);
function openAnalysis() { navigation?.analyze({ ...time.filter.value, harness: props.harness, range: 'custom', start: time.range.value.start, end: time.range.value.end }); }
const preferences = useShellPreferences(props.initialPreferences);
const monitor = ref<InstanceType<typeof CacheMonitor>>();
const recalcOpen = ref(false);
const status = useQuery('harness.collection', async () => unwrap(await bridge.call('collection.status', { details: false })), { events: ['sources.changed', 'collection.tick'], coalesce:true, eventIntervalMs:1000 });
const summary = useQuery('harness.summary', async signal => {
  const range = time.advance();
  return readShared(bridge, 'stats.summary', { harness: props.harness, start: range.start, end: range.end, offsetMinutes: range.offsetMinutes, grain: time.trendGrain.value, sections: ['models'] }, signal);
}, { deps: [time.refreshKey], events: ['cost.changed', { name: 'usage.changed', when: event => event.harnesses.includes(props.harness) }, { name: 'collection.tick', when: () => time.filter.value.range === 'custom' && !!(time.filter.value.startNow || time.filter.value.endNow) }], coalesce: true, eventIntervalMs:5000 });
const auxiliary = useQuery('harness.auxiliary', async () => props.harness === 'codex' ? unwrap(await bridge.call('stats.auxiliaryCosts', {harness:props.harness,start:time.range.value.start,end:time.range.value.end})) : {costPico:'0',models:[],revision:0},{deps:[time.refreshKey],events:['auxiliary.changed'],coalesce:true,eventIntervalMs:5000});
watch(summary.data, value => { if (value) emit('ready'); });
const catalog = useQuery('harness.pricing', async () => unwrap(await bridge.call('pricing.catalog', {metadataOnly:true})), { events: ['pricing.changed'] });
const switchedSelection = ref<PricingSelection>();
const selection = computed(() => switchedSelection.value ?? catalog.data.value?.current[props.harness]);
const currentGroup = computed(() => catalog.data.value?.groups.find(group => group.id === selection.value?.groupId));
watch(catalog.data, snapshot => {
  if (snapshot?.current[props.harness]?.groupId === switchedSelection.value?.groupId && snapshot?.current[props.harness]?.versionId === switchedSelection.value?.versionId) switchedSelection.value = undefined;
});
function switched(value: PricingSelection) {
  // Publish the association before re-reading the catalog so the previous balance disappears immediately.
  switchedSelection.value = value; void catalog.refresh();
}
let scanCheckpoint = summary.checkpoint();
function scanning() { scanCheckpoint = summary.checkpoint(); }
function scanned() { void summary.refreshIfUnchanged(scanCheckpoint); void monitor.value?.refresh(); }
async function select(value: RangePreset) { try { await time.select(value); } catch { /* Error stays on the page. */ } }
async function apply(value: UsageRange) { try { await time.applyCustom(value); } catch { /* Error stays on the page. */ } }
async function filter(patch: Partial<UsageFilter>) { try { await time.saveFilter(patch); } catch { /* Error stays on the page. */ } }
</script>

<template>
  <div class="harness-page">
    <UsageHeader :title="`${harnessNames[harness]} 统计`" :harness="harness" :status="status.data.value" :error="status.error.value?.message" :mode="time.mode.value" :countdown="preferences.snapshot.value.preferences.showScanCountdown" @scanning="scanning" @scanned="scanned">
      <template #balance><VendorBalanceCard :group-id="selection?.groupId" :vendor-id="currentGroup?.vendorId" :group-loading="catalog.loading.value || catalog.refreshing.value" :group-error="catalog.error.value?.message" :timezone="time.mode.value" /></template>
    </UsageHeader>
    <UnpricedNotice :harness="harness" :initial-preferences="initialPreferences" />
    <ReconciliationNotice :harness="harness" />
    <TimeRangeBar :model-value="time.selection.value" :range="time.range.value" :mode="time.mode.value" :custom-start="time.filter.value.start" :custom-end="time.filter.value.end" allow-follow :start-now="time.filter.value.startNow" :end-now="time.filter.value.endNow" @update:model-value="select" @apply="apply">
      <div class="pricing-tools"><CurrentPricingButton :harness="harness" :catalog="catalog.data.value" :loading="catalog.loading.value" :error="catalog.error.value?.message" @switched="switched" @retry="catalog.refresh" /><Button @click="recalcOpen = true">重算历史…</Button></div>
    </TimeRangeBar>
    <InlineAlert v-if="time.error.value" severity="danger" retry @retry="time.retry">{{ time.error.value }}</InlineAlert>
    <InlineAlert v-if="summary.error.value" severity="danger" retry @retry="summary.refresh">{{ summary.error.value.message }}</InlineAlert>
    <MetricsGrid :metrics="summary.data.value?.metrics" :harness="harness" :loading="summary.loading.value" :auxiliary-pico="auxiliary.data.value?.costPico" />
    <InlineAlert v-if="summary.data.value?.historyNotice">{{ summary.data.value.historyNotice }}</InlineAlert>
    <CacheMonitor ref="monitor" :harness="harness" :timezone="time.mode.value" />
    <ModelTable :models="summary.data.value?.models ?? []" :auxiliary="auxiliary.data.value?.models" />
    <HourlyDetails v-if="hourlyAllowed" :harness="harness" :range="time.range.value" :query-key="time.queryKey.value" :models="summary.data.value?.models ?? []" :model-value="time.filter.value.modelKey" @update:model-value="filter({ modelKey: $event })" />
    <section v-else class="surface history-link"><h2>小时明细</h2><p>超过 7 天的小时明细集中在数据统计与分析中查看。</p><Button @click="openAnalysis">在数据统计与分析中查看小时明细</Button></section>
    <RecalcDialog v-if="recalcOpen" :harness="harness" :mode="time.mode.value" @close="recalcOpen = false" />
  </div>
</template>

<style scoped>
.harness-page { display: grid; gap: 28px; }.harness-page > * { min-width: 0; }.pricing-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
</style>
