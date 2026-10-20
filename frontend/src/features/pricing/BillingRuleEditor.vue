<script setup lang="ts">
import { ref, watch } from 'vue';
import { defaultRule, type Rule } from './types';
import PeakHoursDialog from './PeakHoursDialog.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ modelValue?: Rule | null; readonly?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [rule: Rule | null] }>();
const peak = ref(false); const removing = ref(false);
let latest = props.modelValue;
watch(() => props.modelValue, value => { latest = value; }, { flush: 'sync' });
function publish(rule: Rule | null) { latest = rule; emit('update:modelValue', rule); }
function mode(event: Event) { const value = (event.target as HTMLSelectElement).value; if (!value) { if (latest) removing.value = true; } else publish({ ...(latest ?? defaultRule(value as Rule['mode'])), mode: value as Rule['mode'] }); }
function extra(field: 'input' | 'cache' | 'cacheWrite' | 'output', event: Event) { if (latest) publish({ ...latest, rates: { ...latest.rates, [field]: (event.target as HTMLInputElement).value || null } }); }
function threshold(event: Event) { if (latest) publish({ ...latest, thresholdK: Number((event.target as HTMLInputElement).value) }); }
</script>
<template>
  <details class="rule-editor"><summary>计费规则{{ modelValue ? modelValue.mode === 'peak' ? ' · 峰谷' : ' · 阶梯' : '' }}</summary>
    <div class="rule-fields"><label>规则<select aria-label="计费规则" :value="modelValue?.mode ?? ''" :disabled="readonly" @change="mode"><option value="">无附加规则</option><option value="peak">峰谷</option><option value="tier">阶梯</option></select></label>
      <template v-if="modelValue">
        <Button v-if="modelValue.mode === 'peak'" :disabled="readonly" @click="peak = true">峰时与时区（{{ modelValue.hours.length }} 小时 / {{ modelValue.weekdays.length }} 天）</Button>
        <label v-else>阈值 K（含缓存，1K = 1000 token）<input type="number" aria-label="阈值 K" :value="modelValue.thresholdK" min="1" max="1000000" step="1" :disabled="readonly" @input="threshold" /></label>
        <div class="extra-rates"><label v-for="(name, field) in { input: '附加输入', output: '附加输出', cache: '附加缓存命中', cacheWrite: '附加缓存创建' }" :key="field">{{ name }}<input :aria-label="name" :value="modelValue.rates[field] ?? ''" :placeholder="field === 'cacheWrite' ? '0' : '未定价'" inputmode="decimal" :disabled="readonly" @input="extra(field, $event)" /></label></div>
        <p class="text-muted">缓存创建价格留空按 0；其余留空表示未定价，不回退到基础价格。</p>
      </template>
    </div>
    <PeakHoursDialog v-if="peak && modelValue" :rule="modelValue" @close="peak = false" @done="publish($event); peak = false" />
    <ConfirmDialog v-model:open="removing" title="取消计费规则" message="确认移除本行的附加计费规则？保存后从现在起生效。" confirm-label="移除规则" @confirm="publish(null); removing = false" />
  </details>
</template>
<style scoped>.rule-editor { margin-top: 12px; font-size: 12px; }summary { cursor: pointer; color: var(--text-muted); }.rule-fields { display: grid; gap: 12px; padding-top: 12px; }.rule-fields > label { display: grid; gap: 6px; }.extra-rates { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.extra-rates label { display: grid; gap: 6px; }input, select { width: 100%; padding: 6px 8px; border-radius: 4px; }</style>
