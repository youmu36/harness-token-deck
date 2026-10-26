<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from 'vue';
import type { CommandData, RequestMap } from '../../app/ports';
import type { UsageHarness, UsageRange, UsageFilter } from '../usage-common/types';
import type { TimeZoneMode } from '../../shared/time';
import type { ExploreScope } from './scope';
import { emptyScope } from './scope';
import SectionNav from '../../ui/SectionNav.vue';
import { defaultAnalysisView, type AnalysisView } from './analysisState';
const ThroughputReference = defineAsyncComponent(() => import('./ThroughputReference.vue'));
const AnalysisTheme = defineAsyncComponent(() => import('./AnalysisTheme.vue'));
const props = defineProps<{harness:UsageHarness;range:UsageRange;rangeKind?:RequestMap['stats.explore']['rangeKind'];filter:UsageFilter;mode:TimeZoneMode;scope?:ExploreScope;options?:CommandData<'stats.exploreOptions'>;view?:AnalysisView;refreshToken?:number;resolveRange?:() => UsageRange;hideNavigation?:boolean}>();
const fallback = ref(defaultAnalysisView()), view = computed(() => props.view ?? fallback.value);
const section = computed({get:() => view.value.panel,set:value => {view.value.panel = value;}});
const effectiveScope = computed(() => ({...emptyScope(),...props.scope}));
</script>
<template><section class="analysis-page" aria-label="数据分析">
  <SectionNav v-if="!hideNavigation" v-model="section" label="分析主题" :items="[{value:'throughput',label:'TPS'},{value:'cost',label:'成本'},{value:'cache',label:'缓存'},{value:'value',label:'性价比'}]" />
  <ThroughputReference v-if="section === 'throughput'" :scope="effectiveScope" :options="options" :range="range" :resolve-range="resolveRange" :view="view" :refresh-token="refreshToken" />
  <AnalysisTheme :mode="mode" v-else :key="section" :theme="section" :scope="effectiveScope" :range="range" :range-kind="rangeKind" :resolve-range="resolveRange ?? (() => range)" :view="view" :options="options" :refresh-token="refreshToken" />
</section></template>
<style scoped>.analysis-page{display:grid;gap:22px;min-width:0}</style>
