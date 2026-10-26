<script setup lang="ts">
import { ref } from 'vue';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import type { UsageRange } from '../usage-common/types';
import type { AnalysisView } from './analysisState';
import TimeRangeBar from '../usage-common/TimeRangeBar.vue';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ view: AnalysisView; range: UsageRange; recentRange: UsageRange; mode: TimeZoneMode }>();
const selection = ref(props.view.recent.range), error = ref('');
function apply(value: UsageRange) {
  const start = Date.parse(value.start), end = Date.parse(value.end), now = Date.now();
  if (end > now || start < now - 62 * 86400000) { error.value = '自定义范围须在最近 62 天内，结束不能晚于当前时间'; return; }
  error.value = ''; props.view.recent = { ...props.view.recent, range: 'custom', start: value.start, end: value.end };
}
</script>
<template>
  <section class="page-range surface" aria-label="页面时间范围">
    <fieldset :disabled="view.period !== 'range'" class="recent-tools"><legend>近期范围</legend>
      <TimeRangeBar reveal-menus v-model:draft-start="view.draftStart" v-model:draft-end="view.draftEnd" :model-value="selection" :range="recentRange" :mode="mode" :custom-start="view.recent.start" :custom-end="view.recent.end" @update:model-value="selection = $event; $event !== 'custom' && (view.recent.range = $event)" @apply="apply" />
    </fieldset>
    <div class="calendar-tools"><ChoiceMenu reveal-on-open v-model="view.period" label="统计周期" :options="[{value:'range',label:'近期'}, {value:'month',label:'本月'}, {value:'quarter',label:'季度'}, {value:'year',label:'年度'}, {value:'all',label:'全部历史'}]" />
      <label v-if="view.period === 'quarter' || view.period === 'year'">年份<input v-model.number.lazy="view.year" aria-label="历史年份" type="number" min="1971" :max="new Date().getFullYear()" /></label>
      <label v-if="view.period === 'quarter'">季度<select v-model.number="view.quarter" aria-label="历史季度"><option v-for="q in 4" :key="q" :value="q">第 {{ q }} 季度</option></select></label>
      <p>{{ formatDateTime(range.start, mode) }} 至 {{ formatDateTime(range.end, mode) }}</p>
    </div><InlineAlert v-if="error" severity="danger">{{ error }}</InlineAlert>
  </section>
</template>
<style scoped>
.page-range{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,.65fr);gap:22px;padding:18px}.recent-tools{border:0;min-width:0;padding:0;margin:0}.recent-tools:disabled{opacity:.5}legend{font-size:12px;color:var(--text-muted);margin-bottom:12px}.calendar-tools{display:flex;align-content:start;align-items:center;flex-wrap:wrap;gap:12px;border-left:1px solid var(--border);padding-left:22px}.calendar-tools label{display:flex;gap:8px;align-items:center;font-size:12px}.calendar-tools input{width:90px;padding:7px}.calendar-tools select{padding:7px}.calendar-tools p{width:100%;font-size:11px;color:var(--text-muted);overflow-wrap:anywhere}@media(max-width:1000px){.page-range{grid-template-columns:1fr}.calendar-tools{border-left:0;border-top:1px solid var(--border);padding:16px 0 0}}
</style>
<style scoped>
.page-range{grid-template-columns:minmax(0,1fr) auto;gap:14px;padding:12px 16px;max-width:920px}.calendar-tools{padding-left:16px;gap:8px;max-width:300px}.calendar-tools p{font-size:12px}.recent-tools legend{margin-bottom:8px}.recent-tools :deep(.time-range-bar){gap:10px}.recent-tools :deep(.range-label){gap:8px}.recent-tools :deep(.timezone){margin-left:0}@media(max-width:1000px){.page-range{grid-template-columns:1fr}.calendar-tools{max-width:none;padding:12px 0 0}}
</style>
