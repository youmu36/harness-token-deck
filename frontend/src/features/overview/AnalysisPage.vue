<script setup lang="ts">
import { computed, ref } from 'vue';
import type { CommandData } from '../../app/ports';
import type { UsageHarness, UsageRange, UsageFilter } from '../usage-common/types';
import type { TimeZoneMode } from '../../shared/time';
import type { ExploreScope } from './scope';
import { emptyScope } from './scope';
import { calculateRange } from '../usage-common/useTimeRange';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import SectionNav from '../../ui/SectionNav.vue';
import DimensionExplorer from './DimensionExplorer.vue';
import ThroughputReference from './ThroughputReference.vue';
const props = defineProps<{ harness: UsageHarness; range: UsageRange; filter: UsageFilter; mode: TimeZoneMode; scope?: ExploreScope; options?: CommandData<'stats.exploreOptions'>; kind?: 'statistics' | 'quality' }>();
defineEmits<{ 'update:harness': [value: UsageHarness] }>();
const period = ref(props.kind === 'quality' ? 'all' : props.kind === 'statistics' ? 'range' : 'month');
const section = ref('throughput');
const picking = ref(false);
const year = ref(new Date().getFullYear());
const endYear = ref(new Date().getFullYear());
const quarter = ref(Math.floor(new Date().getMonth() / 3) + 1);
const effectiveScope = computed<ExploreScope>(() => ({ ...emptyScope(), ...props.scope, harnesses: props.scope?.harnesses?.length ? props.scope.harnesses : props.harness === 'all' ? [] : [props.harness] }));
function resolveRange(): UsageRange {
  if (period.value === 'range') return calculateRange(props.filter, props.mode);
  const now = new Date(), offset = props.range.offsetMinutes * 60000;
  const local = new Date(now.getTime() + offset);
  const y = period.value === 'month' ? local.getUTCFullYear() : year.value;
  const month = period.value === 'month' ? local.getUTCMonth() : period.value === 'quarter' ? (quarter.value - 1) * 3 : 0;
  const start = period.value === 'all' ? Date.UTC(1971, 0, 1) : Date.UTC(y, month, 1) - offset;
  const end = Math.min(now.getTime(), period.value === 'all' ? now.getTime() : period.value === 'years' ? Date.UTC(endYear.value + 1, 0, 1) - offset : Date.UTC(period.value === 'year' ? y + 1 : y, period.value === 'month' ? month + 1 : period.value === 'quarter' ? month + 3 : 0, 1) - offset);
  return { start: new Date(start).toISOString(), end: new Date(end).toISOString(), offsetMinutes: props.range.offsetMinutes, crossesDst: props.range.crossesDst };
}
const range = computed(resolveRange);
</script>

<template>
  <section class="analysis-page" :aria-label="kind === 'quality' ? '数据缺失查询' : '数据分析'">
    <div class="period-bar"><ChoiceMenu v-model="period" label="分析周期" :options="[{value:'range',label:'统计页所选范围'}, {value:'month',label:'本月'}, {value:'quarter',label:'季度'}, {value:'year',label:'年份'}, {value:'years',label:'多年份'}, {value:'all',label:'全部历史'}]" /><label v-if="['quarter','year','years'].includes(period)">年份<input v-model.number.lazy="year" aria-label="分析年份" type="number" min="1971" :max="new Date().getFullYear()" /></label><label v-if="period === 'years'">结束年份<input v-model.number.lazy="endYear" aria-label="结束年份" type="number" :min="year" :max="new Date().getFullYear()" /></label><label v-if="period === 'quarter'">季度<select v-model.number="quarter" aria-label="分析季度"><option v-for="q in 4" :key="q" :value="q">第 {{ q }} 季度</option></select></label></div>
    <DimensionExplorer v-if="kind" :key="kind" :scope="effectiveScope" :range="range" :resolve-range="resolveRange" :harness="harness" :mode="mode" :options="options" :kind="kind" />
    <template v-else>
      <SectionNav v-model="section" label="分析分区" :items="[{value:'throughput',label:'吞吐与模型'}, {value:'cache',label:'缓存表现'}, {value:'value',label:'模型性价比'}]" />
      <template v-if="section === 'throughput'"><ThroughputReference :scope="effectiveScope" :options="options" @selecting="picking = $event" /><DimensionExplorer v-if="!picking" :scope="effectiveScope" :range="range" :resolve-range="resolveRange" :harness="harness" :mode="mode" :options="options" /></template>
      <DimensionExplorer v-else :key="section" :scope="effectiveScope" :range="range" :resolve-range="resolveRange" :harness="harness" :mode="mode" :options="options" :kind="section as 'cache' | 'value'" />
    </template>
  </section>
</template>

<style scoped>
.analysis-page{display:grid;gap:22px;min-width:0}.period-bar{display:flex;align-items:center;flex-wrap:wrap;gap:14px}.period-bar label{display:flex;align-items:center;gap:8px;font-size:13px}.period-bar input{width:95px;padding:5px}.period-bar select{padding:5px}
</style>
