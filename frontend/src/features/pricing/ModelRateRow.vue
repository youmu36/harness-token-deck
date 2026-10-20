<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue';
import { Trash2 } from 'lucide-vue-next';
import { BridgeKey } from '../../app/ports';
import { unwrap, asAppError } from '../../app/errors';
import type { DraftRow, Pool } from './types';
import BillingRuleEditor from './BillingRuleEditor.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import Button from '../../ui/Button.vue';
import IconButton from '../../ui/IconButton.vue';
const props = withDefaults(defineProps<{ modelValue: DraftRow; pool?: Pool; usedModels?: string[]; readonly?: boolean; removable?: boolean }>(), { removable: true });
const emit = defineEmits<{ 'update:modelValue': [row: DraftRow]; remove: [] }>();
const bridge = inject(BridgeKey)!; const confirm = ref(false); const proposed = ref(''); const error = ref(''); const pending = ref(false);
let latest = props.modelValue;
watch(() => props.modelValue, value => { latest = value; }, { flush: 'sync' });
function publish(row: DraftRow) { latest = row; emit('update:modelValue', row); }
let disposed = false;
onBeforeUnmount(() => { disposed = true; });
const base = computed(() => props.pool?.models.find(item => item.modelId.toLowerCase() === props.modelValue.model.modelId.toLowerCase()));
const modelKey = (value: string) => value.replace(/[\s\uFEFF]/g, '').toLowerCase();
const used = computed(() => new Set(props.usedModels?.filter(Boolean).map(modelKey)));
function unavailable(value: string) { return used.value.has(modelKey(value)); }
function update(patch: Partial<DraftRow['model']>) { publish({ ...latest, model: { ...latest.model, ...patch } }); }
function multiplier(event: Event) { publish({ ...latest, multiplier: (event.target as HTMLInputElement).value }); }
function choose(event: Event) {
  proposed.value = (event.target as HTMLSelectElement).value;
  if (unavailable(proposed.value)) { (event.target as HTMLSelectElement).value = props.modelValue.model.modelId; return; }
  if (proposed.value === props.modelValue.model.modelId) return;
  if ([props.modelValue.model.input, props.modelValue.model.cache, props.modelValue.model.cacheWrite, props.modelValue.model.output].some(value => value != null && value !== '') || props.modelValue.model.billingRule) confirm.value = true;
  else apply();
}
function apply() { if (unavailable(proposed.value)) { confirm.value = false; return; } const selected = props.pool?.models.find(m => m.modelId === proposed.value); update({ modelId: proposed.value, input: selected?.rates.input ?? null, cache: selected?.rates.cache ?? null, cacheWrite: selected?.rates.cacheWrite ?? null, output: selected?.rates.output ?? null, billingRule: null }); confirm.value = false; }
async function fill() {
  if (!base.value || pending.value) return; pending.value = true; error.value = '';
  const modelId = props.modelValue.model.modelId;
  try { const result = unwrap(await bridge.call('models.fillByMultiplier', { rates: { ...base.value.rates }, multiplier: props.modelValue.multiplier })); if (!disposed && modelId === props.modelValue.model.modelId) update(result); }
  catch (failure) { if (!disposed) error.value = asAppError(failure).message; } finally { pending.value = false; }
}
</script>
<template>
  <div class="rate-row">
    <div class="row-fields">
      <label class="model-select">计价模型<select aria-label="计价模型" :value="modelValue.model.modelId" :disabled="readonly || pending" @change="choose"><option value="" disabled>选择模型</option><option v-if="modelValue.model.modelId && !base" :value="modelValue.model.modelId">{{ modelValue.model.modelId }}（已有价格）</option><option v-for="item in pool?.models" :key="item.modelId" :value="item.modelId" :disabled="unavailable(item.modelId)">{{ item.modelId }}{{ unavailable(item.modelId) ? '（已添加）' : '' }}</option></select></label>
      <label v-for="(name, field) in { input: '输入价格', output: '输出价格', cache: '缓存命中价格', cacheWrite: '缓存创建价格' }" :key="field">{{ name }}<input :aria-label="name" :value="modelValue.model[field] ?? ''" inputmode="decimal" :placeholder="field === 'cacheWrite' ? '0' : '未定价'" :disabled="readonly || pending" @input="update({ [field]: ($event.target as HTMLInputElement).value || null })" /></label>
      <IconButton v-if="removable !== false" label="删除模型行" :disabled="readonly || pending" @click="emit('remove')"><Trash2 :size="17" /></IconButton>
    </div>
    <div class="multiplier"><span class="text-muted">模型池价格（输入 / 输出 / 命中 / 创建）：{{ base ? `${base.rates.input ?? '未定价'} / ${base.rates.output ?? '未定价'} / ${base.rates.cache ?? '未定价'} / ${base.rates.cacheWrite ?? '0'}` : '模型池未提供基准价' }}</span><label>倍率<input aria-label="倍率" :value="modelValue.multiplier" inputmode="decimal" :disabled="readonly || pending" @input="multiplier" /></label><Button :disabled="readonly || !base" :pending="pending" @click="fill">按倍率填充</Button><span v-if="error" class="error" role="alert">{{ error }}</span></div>
    <BillingRuleEditor :model-value="modelValue.model.billingRule" :readonly="readonly || pending" @update:model-value="update({ billingRule: $event })" />
    <ConfirmDialog v-model:open="confirm" title="覆盖已填价格" message="选择此模型将覆盖已填的普通价格与附加规则，使用该模型的基准价格。" confirm-label="确认覆盖" @confirm="apply" />
  </div>
</template>
<style scoped>
.rate-row { container-type: inline-size; padding: 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); min-width: 0; }
.rate-row + .rate-row { margin-top: 10px; }
.row-fields { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)) 32px; align-items: end; gap: 12px; }
label { display: grid; gap: 6px; min-width: 0; font-size: 12px; }
.model-select { grid-column: 1 / -1; width: min(240px, 100%); }
input, select { min-width: 0; max-width: 100%; width: 100%; padding: 6px 8px; border-radius: 4px; }
.multiplier { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 12px; font-size: 12px; }
.multiplier label { display: flex; align-items: center; gap: 6px; }
.multiplier input { width: 80px; }
.error { color: var(--danger); }
@container (max-width: 680px) { .row-fields { column-gap: 8px; } }
</style>
