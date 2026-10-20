<script setup lang="ts">
import { inject, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Plus, Save, Undo2 } from 'lucide-vue-next';
import { useEditorDraft } from './useEditorDraft';
import type { Version, Pool } from './types';
import { useCommand } from '../../shared/command';
import { asAppError } from '../../app/errors';
import { NavigationGuardKey } from '../../app/ports';
import { useToast } from '../../ui/useToast';
import ModelRateRow from './ModelRateRow.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ version: Version; pool?: Pool; readonly?: boolean; revision: number }>();
const emit = defineEmits<{ saved: []; conflict: [] }>();
const draft = useEditorDraft(props.version); const error = ref(''); const changedRemotely = ref(false);
const command = useCommand('pricing.saveVersionPrices'); const toast = useToast();
watch(() => props.version, value => { if (!draft.dirtyCount.value) draft.reset(value); else changedRemotely.value = true; });
watch(() => props.version.id, () => { draft.reset(props.version); error.value = ''; changedRemotely.value = false; });
const confirm = ref(false); const confirmMessage = ref(''); let confirmed: (() => void) | undefined;
function requestLeave(action: () => void) { if (command.pending.value) return; if (!draft.dirtyCount.value) { action(); return; } confirmed = () => { draft.reset(props.version); action(); }; confirmMessage.value = '未保存的价格修改将被放弃，是否继续？'; confirm.value = true; }
function add() { draft.rows.value.push({ key: crypto.randomUUID(), model: { modelId: '', input: null, cache: null, output: null, billingRule: null }, multiplier: '' }); }
function discard() { requestLeave(() => { error.value = ''; changedRemotely.value = false; }); }
function remove(index: number) { confirmed = () => { draft.rows.value.splice(index, 1); }; confirmMessage.value = '确认删除此模型行？保存后从现在起生效。'; confirm.value = true; }
function confirmAction() { confirm.value = false; confirmed?.(); confirmed = undefined; }
function unload(event: BeforeUnloadEvent) { if (draft.dirtyCount.value) { event.preventDefault(); event.returnValue = ''; } }
const releaseGuard = inject(NavigationGuardKey, undefined)?.(requestLeave);
onMounted(() => { window.addEventListener('beforeunload', unload); });
onBeforeUnmount(() => { releaseGuard?.(); window.removeEventListener('beforeunload', unload); });
async function save() {
  if (!draft.dirtyCount.value || props.readonly || command.pending.value) return; error.value = '';
  try { await command.run({ versionId: props.version.id, models: draft.models.value, hints: draft.hints.value, expectedRevision: props.revision }); draft.accept(); changedRemotely.value = false; toast.success('价格修改已保存'); emit('saved'); }
  catch (failure) { error.value = asAppError(failure).message; if (asAppError(failure).code === 'PRICING_REVISION_CONFLICT') { changedRemotely.value = true; emit('conflict'); } }
}
defineExpose({ requestLeave });
</script>
<template>
  <section class="price-editor" aria-label="价格编辑器">
    <header><h3>模型价格 · USD / 百万 token</h3><Button :disabled="readonly || command.pending.value || draft.rows.value.length >= 500" @click="add"><Plus :size="15" />添加模型行</Button></header>
    <InlineAlert v-if="readonly" severity="info">此分组已停用</InlineAlert>
    <InlineAlert v-if="changedRemotely" severity="warning">目录已变化，草稿仍保留；请检查最新目录后重新确认保存。</InlineAlert>
    <ModelRateRow v-for="(row, index) in draft.rows.value" :key="row.key" :model-value="row" :pool="pool" :used-models="draft.rows.value.filter(other => other.key !== row.key).map(other => other.model.modelId)" :readonly="readonly || command.pending.value" @update:model-value="draft.rows.value[index] = $event" @remove="remove(index)" />
    <p v-if="!draft.rows.value.length" class="empty text-muted">此版本暂无模型价格</p>
    <div><Button :disabled="readonly || command.pending.value || draft.rows.value.length >= 500" @click="add"><Plus :size="15" />添加模型行</Button></div>
    <InlineAlert v-if="error" severity="danger">{{ error }}</InlineAlert>
    <footer><div><p>未保存修改 {{ draft.dirtyCount.value }} 项</p><p class="text-muted">{{ draft.dirtyCount.value && !draft.pricingDirty.value ? '仅更新倍率提示，不产生修订' : '保存会追加一条价格修订，从现在起生效；已入账的记录不变。' }}</p></div><div class="actions"><Button :disabled="!draft.dirtyCount.value || command.pending.value" @click="discard"><Undo2 :size="15" />放弃修改</Button><Button variant="primary" :pending="command.pending.value" :disabled="!draft.dirtyCount.value || readonly" @click="save"><Save :size="15" />{{ draft.dirtyCount.value && !draft.pricingDirty.value ? '保存倍率提示' : '保存（追加修订）' }}</Button></div></footer>
    <ConfirmDialog v-model:open="confirm" title="确认修改" :message="confirmMessage" confirm-label="确认" @confirm="confirmAction" />
  </section>
</template>
<style scoped>.price-editor { min-width: 0; display: grid; gap: 12px; padding: 18px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }header, footer, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }header, footer { justify-content: space-between; }footer { border-top: 1px solid var(--border); padding-top: 16px; }.empty { padding: 24px 0; }button { display: inline-flex; align-items: center; gap: 6px; }footer p { font-size: 12px; }footer .text-muted { margin-top: 5px; }</style>
