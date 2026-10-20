<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { BridgeKey } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { harnessNames, versionName, type Catalog, type Harness, type PricingSelection } from './types';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ open: boolean; harness: Harness; catalog: Catalog }>();
const emit = defineEmits<{ 'update:open': [value: boolean]; switched: [value: PricingSelection] }>();
const bridge = inject(BridgeKey)!;
const groupId = ref(''); const versionId = ref(''); const error = ref('');
const groups = computed(() => props.catalog.groups.filter(group => !group.disabled && !group.historical));
const group = computed(() => groups.value.find(group => group.id === groupId.value));
const versions = computed(() => group.value?.versions.filter(version => !version.disabled) ?? []);
watch(() => props.open, open => {
  if (!open) return;
  groupId.value = props.catalog.current[props.harness]?.groupId ?? groups.value[0]?.id ?? '';
  versionId.value = props.catalog.current[props.harness]?.versionId ?? versions.value.find(version => version.current)?.id ?? '';
  error.value = '';
}, { immediate: true });
function selectGroup(event: Event) {
  groupId.value = (event.target as HTMLSelectElement).value;
  versionId.value = versions.value.find(version => version.current)?.id ?? '';
  error.value = '';
}
const valid = computed(() => !!group.value && versions.value.some(version => version.id === versionId.value));
const preview = useQuery('pricing.previewSwitch', async () => unwrap(await bridge.call('pricing.previewSwitch', { harness: props.harness, groupId: groupId.value, versionId: versionId.value })), {
  visible: () => props.open && valid.value, deps: [groupId, versionId, () => props.harness], clearOnChange: true, events: ['pricing.changed'],
});
const command = useCommand('pricing.switchCurrent');
const unchanged = computed(() => preview.data.value?.changed === false);
const canConfirm = computed(() => valid.value && preview.data.value?.changed && !preview.loading.value && !preview.refreshing.value && !preview.error.value);
const versionChanges = computed(() => versions.value.find(version => version.current)?.id !== versionId.value);
watch([groupId, versionId], () => { error.value = ''; });
async function confirm() {
  if (!canConfirm.value || command.pending.value) return;
  error.value = '';
  const selected = { groupId: groupId.value, versionId: versionId.value };
  try {
    await command.run({ harness: props.harness, ...selected, expectedRevision: preview.data.value!.revision });
    emit('switched', selected); emit('update:open', false);
  } catch (failure) {
    error.value = asAppError(failure).message;
    if (asAppError(failure).code === 'PRICING_REVISION_CONFLICT') void preview.refresh();
  }
}
</script>

<template>
  <Dialog :open="open" title="切换价格组与版本" :pending="command.pending.value" :error="error || preview.error.value?.message" @update:open="emit('update:open', $event)">
    <div class="switch-fields">
      <label>价格组<select aria-label="价格组" :value="groupId" :disabled="command.pending.value" @change="selectGroup"><option v-for="item in groups" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
      <label>版本<select v-model="versionId" aria-label="版本" :disabled="command.pending.value"><option v-for="item in versions" :key="item.id" :value="item.id">{{ versionName(item) }}{{ item.current ? '（当前生效）' : '' }}</option></select></label>
    </div>
    <div class="switch-impact" aria-live="polite">
      <p>确认后，{{ harnessNames[harness] }} 之后入账的请求按所选组与版本计费。</p>
      <p v-if="preview.loading.value || preview.refreshing.value">正在读取影响范围…</p>
      <p v-else-if="preview.data.value && versionChanges">切换版本会影响使用此组的所有 Harness：{{ preview.data.value.affectedHarnesses.map(item => harnessNames[item]).join('、') || '无' }}。</p>
      <p v-else-if="preview.data.value">此组当前生效版本保持不变。</p>
      <p>已入账数据不会自动重算。</p>
      <p v-if="unchanged" class="text-muted">与当前设置相同</p>
      <Button v-if="preview.error.value" @click="preview.refresh">重新读取影响范围</Button>
    </div>
    <template #footer><Button :disabled="command.pending.value" @click="emit('update:open', false)">取消</Button><Button variant="primary" :pending="command.pending.value" :disabled="!canConfirm" @click="confirm">确认切换</Button></template>
  </Dialog>
</template>

<style scoped>
.switch-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }.switch-fields label { display: grid; gap: 8px; min-width: 0; }.switch-fields select { width: 100%; min-height: 36px; padding: 6px 10px; border-radius: 6px; }
.switch-impact { display: grid; gap: 8px; margin-top: 20px; font-size: 13px; }
</style>
