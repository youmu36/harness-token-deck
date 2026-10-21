<script setup lang="ts">
import { computed, inject, ref, watch, type DeepReadonly } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { rate, type DraftRow } from './types';
import ModelRateRow from './ModelRateRow.vue';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
type Item = DeepReadonly<CommandData<'pricing.unpriced'>['items'][number]>;
const props = defineProps<{ item: Item; action: 'price' | 'assign' | 'ignore'; pricingRevision: number; usageRevision: number }>();
const emit = defineEmits<{ close: []; saved: []; refresh: [] }>();
const bridge = inject(BridgeKey)!;
const catalog = useQuery('unpriced.catalog', () => bridge.call('pricing.catalog', {}).then(unwrap), { events: ['models.changed', 'pricing.changed'] });
const pool = useQuery('unpriced.pool', () => bridge.call('models.pool', {}).then(unwrap), { events: ['models.changed', 'pricing.changed'] });
const groups = computed(() => catalog.data.value?.groups.filter(g => !g.disabled) ?? []);
const groupId = ref(props.item.groupId ?? 'htd-history');
const versionId = ref(props.item.versionId ?? 'htd-history-v1');
const versions = computed(() => groups.value.find(g => g.id === groupId.value)?.versions.filter(v => !v.disabled) ?? []);
const draft = ref<DraftRow>({ key: props.item.id, multiplier: props.item.multiplier || '1', model: props.item.price ? JSON.parse(JSON.stringify(props.item.price)) : { modelId: props.item.modelId ?? '', input: null, cache: null, cacheWrite: null, output: null, billingRule: null } });
const recordLimit = ref(props.item.recordCount);
const command = useCommand('pricing.resolveUnpriced'); const error = ref('');
const title = { price: '编辑未定价模型价格', assign: '编辑价格组与版本', ignore: '忽略未定价请求' };
function changeGroup() { versionId.value = groupId.value === 'htd-history' ? 'htd-history-v1' : versions.value[0]?.id ?? ''; }
function loadVersion() {
  const model = versions.value.find(v => v.id === versionId.value)?.models.find(m => m.modelId.toLowerCase() === (props.item.modelId ?? '').toLowerCase());
  if (model) draft.value.model = JSON.parse(JSON.stringify(model));
}
let initialized = false;
watch(() => catalog.data.value, value => {
  if (!value || initialized) return;
  initialized = true;
  if (!groups.value.some(g => g.id === groupId.value)) { groupId.value = 'htd-history'; changeGroup(); }
  else if (!versions.value.some(v => v.id === versionId.value)) changeGroup();
  if (!props.item.price) loadVersion();
}, { immediate: true });
const valid = computed(() => props.action === 'ignore' ? Number.isInteger(recordLimit.value) && recordLimit.value > 0 && recordLimit.value <= props.item.recordCount : !!versionId.value && (props.action !== 'price' || !!draft.value.model.modelId.trim()));
async function submit() {
  if (!valid.value || command.pending.value) return;
  error.value = '';
  try {
    const model = draft.value.model;
    await command.run({ itemIds: [props.item.id], action: props.action, expectedPricingRevision: props.pricingRevision, expectedUsageRevision: props.usageRevision,
      ...(props.action === 'ignore' ? { recordLimit: recordLimit.value } : { groupId: groupId.value, versionId: versionId.value }),
      ...(props.action === 'price' ? { model: { ...model, modelId: model.modelId.trim(), input: rate(model.input), cache: rate(model.cache), cacheWrite: rate(model.cacheWrite), output: rate(model.output) }, multiplier: draft.value.multiplier } : {}) });
    emit('saved'); emit('close');
  } catch (failure) { const issue = asAppError(failure); error.value = issue.message; if (['PRICING_REVISION_CONFLICT', 'USAGE_REVISION_CONFLICT', 'UNPRICED_CHANGED'].includes(issue.code)) { emit('refresh'); await catalog.refresh(); } }
}
</script>
<template>
  <Dialog :open="true" :title="title[action]" :pending="command.pending.value" :error="error || catalog.error.value?.message || pool.error.value?.message" @update:open="emit('close')">
    <form id="unpriced-resolution" class="resolution-fields" @submit.prevent="submit">
      <p>{{ item.modelId ?? '未知模型' }} · {{ item.recordCount }} 条请求</p>
      <template v-if="action === 'ignore'">
        <label>忽略条数<input v-model.number="recordLimit" aria-label="忽略条数" type="number" min="1" :max="item.recordCount" :disabled="command.pending.value" /></label>
        <p class="text-muted">按时间从早到晚忽略所选条数，保留原用量与费用。可在“显示已忽略”中恢复；之后新增的请求仍会提示。</p>
      </template>
      <template v-else>
        <div class="targets"><label>价格组<select v-model="groupId" aria-label="价格组" :disabled="command.pending.value" @change="changeGroup"><option value="htd-history">历史价格组</option><option v-for="group in groups.filter(g => g.id !== 'htd-history')" :key="group.id" :value="group.id">{{ group.name }}</option></select></label>
          <label>版本<select v-model="versionId" aria-label="价格版本" :disabled="command.pending.value" @change="loadVersion"><option v-if="groupId === 'htd-history' && !versions.length" value="htd-history-v1">v1</option><option v-for="version in versions" :key="version.id" :value="version.id">{{ version.displayName }}</option></select></label></div>
        <p class="text-muted">历史价格组仅用于统计与分析，不关联供应商、不写入资金流水。改选其他价格组也不会把历史消费转给新的供应商。</p>
        <template v-if="action === 'price'"><p>价格单位：USD / 百万 tokens。保存后返回列表，一键重算已补价请求。</p><ModelRateRow v-model="draft" :pool="pool.data.value" :removable="false" :readonly="command.pending.value" /><p class="text-muted">价格会同步保存到所选版本；同版本的其他待补价请求也会使用该价格。</p></template>
        <p v-else class="text-muted">保存归属后，按所选版本中的最新模型价格判断是否可补价。</p>
      </template>
    </form>
    <template #footer><Button :disabled="command.pending.value" @click="emit('close')">取消</Button><Button variant="primary" type="submit" form="unpriced-resolution" :pending="command.pending.value" :disabled="!valid">{{ action === 'ignore' ? '确认忽略' : '保存' }}</Button></template>
  </Dialog>
</template>
<style scoped>.resolution-fields { display: grid; gap: 16px; }.resolution-fields p { font-size: 13px; line-height: 1.7; }.targets { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }label { display: grid; gap: 7px; font-size: 13px; }select, input { min-width: 0; padding: 8px; border-radius: 5px; width: 100%; }</style>
