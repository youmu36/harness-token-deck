<script setup lang="ts">
import { computed, defineAsyncComponent, inject, ref } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { UsageNavigationKey } from '../../app/usageNavigation';
import { unwrap } from '../../app/errors';
import { useShellPreferences } from '../../app/useShellPreferences';
import { useQuery } from '../../shared/query';
import type { UsageFilter } from '../usage-common/types';
import SectionNav from '../../ui/SectionNav.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Button from '../../ui/Button.vue';
import ScopeFilters from './ScopeFilters.vue';
import PageRangeBar from './PageRangeBar.vue';
import { useAnalysisState } from './analysisState';
import { usePageRange } from './pageRange';
import { emptyScope } from './scope';
const StatisticsPanel = defineAsyncComponent(() => import('./StatisticsPanel.vue'));
const AnalysisPage = defineAsyncComponent(() => import('./AnalysisPage.vue'));
const props = defineProps<{initialPreferences:CommandData<'preferences.get'>;analysisFilter?:Partial<UsageFilter>;canReturn?:boolean}>();
const bridge = inject(BridgeKey)!, navigation = inject(UsageNavigationKey,undefined);
const preferences = useShellPreferences(props.initialPreferences);
const statistics = useAnalysisState(props.initialPreferences,'statisticsView');
const analysis = useAnalysisState(props.initialPreferences);
if (props.analysisFilter) {
  statistics.view.scope.harnesses = props.analysisFilter.harness && props.analysisFilter.harness !== 'all' ? [props.analysisFilter.harness] : [];
  statistics.view.scope.modelKeys = props.analysisFilter.modelKey == null ? [] : [props.analysisFilter.modelKey];
  statistics.view.draft = JSON.parse(JSON.stringify(statistics.view.scope));
  statistics.view.recent = {...statistics.view.recent,...props.analysisFilter}; statistics.view.period = 'range'; analysis.view.section = 'statistics';
}
const section = computed({get:() => analysis.view.section === 'analysis' ? 'analysis' : 'statistics',set:value => {analysis.view.section = value as 'statistics'|'analysis';}});
const view = computed(() => section.value === 'statistics' ? statistics.view : analysis.view);
const mode = computed(() => preferences.snapshot.value.preferences.timezone);
const statisticsTime = usePageRange(statistics.view,() => mode.value), analysisTime = usePageRange(analysis.view,() => mode.value);
const time = computed(() => section.value === 'statistics' ? statisticsTime : analysisTime);
const scope = computed(() => view.value.scope);
const source = computed(() => scope.value.harnesses.length === 1 ? scope.value.harnesses[0]! : 'all');
const refreshToken = ref(0);
const optionsQuery = useQuery('overview.options',async () => unwrap(await bridge.call('stats.exploreOptions',{modelKeys:[...new Set([...scope.value.modelKeys,...(section.value === 'analysis' ? [view.value.tpsModel,...view.value.tpsCompareIds,view.value.focus].filter((id):id is string => id !== null) : [])])],modelLimit:section.value === 'statistics' ? 1 : 100})),{deps:[section,() => JSON.stringify(scope.value.modelKeys)],events:['pricing.changed','models.changed',{name:'wallet.changed',when:event => event.catalogChanged === true}],coalesce:true});
const controlsOpen = ref(false);
const panel = computed({get:() => analysis.view.panel,set:value => {analysis.view.panel = value;}});
</script>
<template><div class="overview-page">
  <Button v-if="canReturn" class="return-link" @click="navigation?.back()">返回 Harness 统计</Button>
  <div class="overview-toolbar surface" @keydown.esc="controlsOpen = false">
    <SectionNav v-model="section" label="统计分析子页" :items="[{value:'statistics',label:'数据统计'},{value:'analysis',label:'数据分析'}]" />
    <SectionNav v-if="section === 'analysis'" v-model="panel" label="分析主题" :items="[{value:'throughput',label:'TPS'},{value:'cost',label:'成本'},{value:'cache',label:'缓存'},{value:'value',label:'性价比'}]" />
    <Button :aria-expanded="controlsOpen" aria-controls="overview-controls" @click="controlsOpen = !controlsOpen">{{ controlsOpen ? '收起筛选' : '筛选与时间' }}</Button>
  <section v-show="controlsOpen" id="overview-controls" class="overview-controls" aria-label="筛选与时间">
  <ScopeFilters :key="section" simple :model-value="scope" :draft-value="view.draft" :options="optionsQuery.data.value" @update:model-value="view.scope = {...emptyScope(),...$event} as typeof view.scope" @update:draft-value="view.draft = {...emptyScope(),...$event} as typeof view.draft" />
  <p v-if="scope.modelKeys.length" class="carried-filter">带入的模型条件：{{ scope.modelKeys.join('、') }} <Button @click="view.scope.modelKeys = []; view.draft.modelKeys = []">清除带入条件</Button></p>
  <PageRangeBar :key="section" :view="view" :range="time.range.value" :recent-range="time.recent.value" :mode="mode" />
  </section>
  </div>
  <InlineAlert v-if="statistics.error.value || analysis.error.value" severity="danger" retry @retry="analysis.retry">{{ statistics.error.value || analysis.error.value }}</InlineAlert>
  <InlineAlert v-if="optionsQuery.error.value" severity="danger" retry @retry="optionsQuery.refresh">{{ optionsQuery.error.value.message }}</InlineAlert>
  <StatisticsPanel v-if="section === 'statistics'" :scope="scope" :range="time.range.value" :range-kind="time.rangeKind.value" :resolve-range="time.advance" :refresh-token="refreshToken" />
  <AnalysisPage v-else hide-navigation :scope="scope" :options="optionsQuery.data.value" :harness="source" :range="time.range.value" :filter="view.recent" :range-kind="time.rangeKind.value" :resolve-range="time.advance" :mode="mode" :view="view" :refresh-token="refreshToken" />
</div></template>
<style scoped>.overview-page,.overview-controls{display:grid;gap:14px;min-width:0}.overview-page>*{min-width:0}.overview-toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:10px 20px;position:sticky;top:0;z-index:15;padding:10px 12px;box-shadow:0 4px 12px #0001}.overview-toolbar>.button{margin-left:auto}.overview-controls{position:absolute;left:0;right:0;top:calc(100% + 6px);padding:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:0 8px 24px #0003;max-height:calc(100vh - 180px);overflow:auto}.return-link{justify-self:start}.carried-filter{display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:12px}</style>
