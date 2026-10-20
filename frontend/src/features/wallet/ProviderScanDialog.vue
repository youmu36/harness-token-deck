<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { asAppError } from '../../app/errors';
import { useCommand } from '../../shared/command';
import { useTask } from '../../shared/task';
import { diagnosticText } from '../usage-common/collectionNotes';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const emit = defineEmits<{ close: []; saved: [id: string] }>();
const open = ref(true); const error = ref('');
const scan = useCommand('wallet.scanProviders'); const create = useCommand('wallet.createScannedVendors'); const task = useTask();
type Row = { id: string; identifier: string; label: string | null; url: string | null; vendorId: string | null; fromCurrentConfig: boolean; name: string; selected: boolean };
const rows = ref<Row[]>([]);
const result = computed(() => task.task.data.value?.status === 'completed' ? task.task.data.value.result : undefined);
watch(result, value => {
  if (!Array.isArray(value?.providers)) return;
  rows.value = value.providers.flatMap(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item) || typeof item.id !== 'string' || typeof item.identifier !== 'string') return [];
    return [{ id: item.id, identifier: item.identifier, label: typeof item.label === 'string' ? item.label : null, url: typeof item.url === 'string' ? item.url : null, vendorId: typeof item.vendorId === 'string' ? item.vendorId : null, fromCurrentConfig: item.fromCurrentConfig === true, name: '', selected: !item.vendorId }];
  });
});
const selected = computed(() => rows.value.filter(row => row.selected && !row.vendorId));
const namesValid = computed(() => selected.value.length > 0 && selected.value.every(row => row.name.trim().length > 0 && row.name.trim().length <= 60) && new Set(selected.value.map(row => row.name.trim().toLocaleLowerCase())).size === selected.value.length);
const busy = computed(() => scan.pending.value || task.active.value || create.pending.value);
async function start() { error.value = ''; rows.value = []; task.taskId.value = ''; try { task.taskId.value = (await scan.run({})).taskId; } catch (failure) { error.value = asAppError(failure).message; } }
async function save() {
  if (!namesValid.value || busy.value) return;
  error.value = '';
  try { const providers = selected.value.map(row => ({ id: row.id, name: row.name.trim() })); const saved = await create.run({ taskId: task.taskId.value, providers: [providers[0]!, ...providers.slice(1)] }); emit('saved', saved.vendorId); open.value = false; }
  catch (failure) { error.value = asAppError(failure).message; }
}
async function cancel() { try { await task.cancel(); } catch (failure) { error.value = asAppError(failure).message; } }
watch(open, value => { if (!value) emit('close'); });
void start();
</script>
<template>
  <Dialog v-model:open="open" title="扫描近 3 天的供应商" :pending="busy" :error="error || task.task.error.value?.message || task.task.data.value?.error?.message">
    <p>扫描近三天记录中的供应商网址，已按网址合并并排除已有供应商。为选中站点填写名称后创建，随后关联价格组。此扫描只在手动点击时运行。</p>
    <div v-if="task.active.value" role="status"><p>已读取 {{ task.task.data.value?.completed ?? 0 }} / {{ task.task.data.value?.total ?? 0 }} 个文件</p><progress :value="task.task.data.value?.completed ?? 0" :max="task.task.data.value?.total || 1" /><Button :disabled="task.cancelling.value" @click="cancel">取消扫描</Button></div>
    <div v-if="result" class="results">
      <section v-for="row in rows" :key="row.id" class="provider-result surface">
        <label><input v-model="row.selected" type="checkbox" :disabled="!!row.vendorId || create.pending.value" :aria-label="`选择 ${row.identifier}`" /><strong>{{ row.url || row.identifier }}</strong></label>
        <p v-if="row.label" class="text-muted">{{ row.label }}</p>
        <p v-if="row.fromCurrentConfig" class="text-muted">网址由记录中的供应商与当前配置对应得到，请确认站点是否正确。</p>
        <p v-if="row.vendorId">已保存为供应商</p>
        <label v-else>供应商名称（必填）<input v-model="row.name" :aria-label="`供应商名称 ${row.identifier}`" :disabled="create.pending.value || !row.selected" maxlength="60" placeholder="填写便于识别的名称" :required="row.selected" /></label>
      </section>
      <p v-if="!rows.length">近 3 天没有可新增的供应商网址。</p>
      <InlineAlert v-if="result.missingProviderCount">{{ result.missingProviderCount }} 条请求没有可解析的供应商网址，已跳过；可手动创建供应商。</InlineAlert>
      <InlineAlert v-if="Array.isArray(result.diagnostics) && result.diagnostics.length" severity="warning">{{ result.diagnostics.filter((d): d is string => typeof d === 'string').map(diagnosticText).join('；') }}</InlineAlert>
    </div>
    <template #footer><Button :disabled="busy" @click="open = false">关闭</Button><Button :disabled="busy" @click="start">重新扫描</Button><Button variant="primary" :pending="create.pending.value" :disabled="!namesValid || task.active.value || scan.pending.value" @click="save">创建选中供应商（{{ selected.length }}）</Button></template>
  </Dialog>
</template>
<style scoped>
.results { display: grid; gap: 12px; margin-top: 16px; max-height: 48vh; overflow-y: auto; }.provider-result { padding: 14px; display: grid; gap: 10px; }
label { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }strong, p { overflow-wrap: anywhere; }input:not([type=checkbox]) { flex: 1; min-width: 160px; padding: 6px 8px; }.text-muted { font-size: 12px; }progress { width: 100%; margin-block: 12px; }
</style>
