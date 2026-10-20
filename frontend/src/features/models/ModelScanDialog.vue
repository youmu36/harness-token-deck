<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { asAppError } from '../../app/errors';
import { useCommand } from '../../shared/command';
import { useTask } from '../../shared/task';
import { diagnosticText } from '../usage-common/collectionNotes';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ revision: number; refresh: () => Promise<void> }>();
const emit = defineEmits<{ close: []; saved: [] }>();
const open = ref(true); const error = ref(''); const selected = ref<string[]>([]);
const scan = useCommand('models.scan'); const add = useCommand('models.add'); const task = useTask();
const revision = ref(props.revision);
const result = computed(() => {
  if (task.task.data.value?.status !== 'completed') return undefined;
  const value = task.task.data.value.result;
  if (!value || !Array.isArray(value.models) || typeof value.revision !== 'number') return undefined;
  const models = value.models.filter((m): m is { modelId: string; inPool: boolean; excluded: boolean } =>
    !!m && typeof m === 'object' && !Array.isArray(m) && typeof m.modelId === 'string' && typeof m.inPool === 'boolean' && typeof m.excluded === 'boolean');
  return { models, revision: value.revision, diagnostics: Array.isArray(value.diagnostics) ? value.diagnostics.filter((d): d is string => typeof d === 'string') : [] };
});
watch(result, value => { if (value) { selected.value = value.models.filter(m => !m.inPool && !m.excluded).map(m => m.modelId); revision.value = value.revision; } });
async function start() { error.value = ''; selected.value = []; task.taskId.value = ''; try { task.taskId.value = (await scan.run({ harness: 'all' })).taskId; } catch (failure) { error.value = asAppError(failure).message; } }
async function save() { if (!selected.value.length) return; error.value = ''; try { await add.run({ modelIds: [selected.value[0]!, ...selected.value.slice(1)], expectedRevision: revision.value }); emit('saved'); open.value = false; } catch (failure) { error.value = asAppError(failure).message; if (asAppError(failure).code === 'PRICING_REVISION_CONFLICT') { await props.refresh(); revision.value = props.revision; error.value += '；已刷新目录，请检查选择后重新确认。'; } } }
async function cancel() { try { await task.cancel(); } catch (failure) { error.value = asAppError(failure).message; } }
const busy = computed(() => scan.pending.value || task.active.value || add.pending.value);
watch(open, value => { if (!value) emit('close'); });
void start();
</script>
<template><Dialog v-model:open="open" title="从来源扫描近 3 天" :pending="busy" :error="error || task.task.error.value?.message || task.task.data.value?.error?.message">
  <p>只读取来源中的模型名称，选择后加入模型池。</p>
  <div v-if="task.active.value" role="status"><p>已读取 {{ task.task.data.value?.completed ?? 0 }} 个文件<span v-if="task.task.data.value?.total"> / {{ task.task.data.value.total }}</span></p><progress :value="task.task.data.value?.completed ?? 0" :max="task.task.data.value?.total || 1" /><Button :disabled="task.cancelling.value || task.task.data.value?.cancelRequested" @click="cancel">{{ task.task.data.value?.cancelRequested ? '正在取消…' : '取消扫描' }}</Button></div>
  <p v-if="task.task.data.value?.status === 'cancelled'">扫描已取消</p>
  <div v-if="result" class="results"><label><input type="checkbox" aria-label="全选扫描结果" :disabled="add.pending.value || !result.models.some(m => !m.inPool)" :checked="!!selected.length && result.models.filter(m => !m.inPool).every(m => selected.includes(m.modelId))" @change="selected = ($event.target as HTMLInputElement).checked ? result.models.filter(m => !m.inPool).map(m => m.modelId) : []" />全选</label><label v-for="model in result.models" :key="model.modelId"><input v-model="selected" type="checkbox" :value="model.modelId" :disabled="model.inPool || add.pending.value" />{{ model.modelId }}<span class="text-muted">{{ model.inPool ? '已在模型池' : model.excluded ? '被移出过 · 加入将恢复' : '' }}</span></label><p v-if="!result.models.length">近 3 天未发现模型</p><InlineAlert v-if="result.diagnostics.length" severity="warning">{{ result.diagnostics.map(diagnosticText).join('；') }}</InlineAlert></div>
  <template #footer><Button :disabled="busy" @click="open = false">关闭</Button><Button v-if="!task.active.value" :disabled="add.pending.value" @click="start">重新扫描</Button><Button variant="primary" :pending="add.pending.value" :disabled="!selected.length || task.active.value" @click="save">加入选中模型（{{ selected.length }}）</Button></template>
</Dialog></template>
<style scoped>.results { display: grid; gap: 12px; margin-top: 16px; max-height: 45vh; overflow: auto; }label { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; overflow-wrap: anywhere; }progress { width: 100%; margin-block: 12px; }</style>
