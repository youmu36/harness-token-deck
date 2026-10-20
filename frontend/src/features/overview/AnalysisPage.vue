<script setup lang="ts">
import { ref } from 'vue';
import type { UsageHarness, UsageRange, UsageFilter } from '../usage-common/types';
import type { TimeZoneMode } from '../../shared/time';
import ChoiceMenu from '../../ui/ChoiceMenu.vue';
import AnalyticsPanel from './AnalyticsPanel.vue';
defineProps<{ harness: UsageHarness; range: UsageRange; filter: UsageFilter; mode: TimeZoneMode }>();
const emit = defineEmits<{ 'update:harness': [value: UsageHarness] }>();
const period = ref<'range' | 'month' | 'quarter' | 'year' | 'years' | 'all'>('month');
</script>

<template>
  <section class="analysis-page" aria-label="数据分析">
    <p class="text-muted">比较模型用量与成本，查看最近 30 天的输出吞吐参考。</p>
      <ChoiceMenu v-model="period" label="分析周期" :options="[{value:'range',label:'统计页所选范围'}, {value:'month',label:'本月'}, {value:'quarter',label:'季度'}, {value:'year',label:'年份'}, {value:'years',label:'多年份'}, {value:'all',label:'全部历史'}]" />
      <AnalyticsPanel :period="period" :models="true" :harness="harness" :range="range" :filter="filter" :mode="mode" @update:harness="emit('update:harness', $event)" />
  </section>
</template>

<style scoped>
.analysis-page{display:grid;gap:24px;min-width:0}.analysis-page>p{font-size:13px}
</style>
