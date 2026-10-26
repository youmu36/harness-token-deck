<script setup lang="ts">
import { computed, inject, ref } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import Button from '../../ui/Button.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const bridge = inject(BridgeKey)!;
const query = useQuery('dataDirectory.status', () => bridge.call('dataDirectory.status', {}).then(unwrap));
const target = ref(''), error = ref(''), busy = ref(false), confirming = ref(false), completed = ref(false);
const preview = ref<CommandData<'dataDirectory.preview'>>();
const pending = ref<RequestMap['dataDirectory.move']>();
const message = computed(() => preview.value ? `将数据库、已有备份及导入资料迁移到 ${preview.value.target}。需要约 ${(preview.value.requiredBytes / 1048576).toFixed(1)} MiB，校验成功后重启。旧副本会保留，直到你手动清理。` : '');
async function pick() {
  error.value = '';
  try { const result = unwrap(await bridge.call('app.pickPath', { kind: 'dir', initial: target.value || query.data.value?.root })); if (result.path) { target.value = result.path; preview.value = undefined; pending.value = undefined; } }
  catch (failure) { error.value = asAppError(failure).message; }
}
async function preflight() {
  error.value = ''; busy.value = true;
  try {
    preview.value = unwrap(await bridge.call('dataDirectory.preview', { target: target.value }));
    if (preview.value.unchanged) { error.value = '目标与当前目录相同，无需迁移'; return; }
    pending.value = { target: preview.value.target, fingerprint: preview.value.fingerprint, operationId: preview.value.resumeOperationId ?? crypto.randomUUID() };
    confirming.value = true;
  } catch (failure) { error.value = asAppError(failure).message; }
  finally { busy.value = false; }
}
async function move() {
  if (!pending.value) return;
  busy.value = true; error.value = '';
  try { const result = unwrap(await bridge.call('dataDirectory.move', pending.value)); completed.value = result.restartRequired; confirming.value = false; }
  catch (failure) { error.value = asAppError(failure).message; }
  finally { busy.value = false; }
}
async function cleanup() {
  busy.value = true; error.value = '';
  try { unwrap(await bridge.call('dataDirectory.cleanup', { operationId: crypto.randomUUID() })); await query.refresh(); }
  catch (failure) { error.value = asAppError(failure).message; }
  finally { busy.value = false; }
}
</script>
<template>
  <section class="settings-card" aria-label="数据目录迁移"><h2>数据目录</h2>
    <template v-if="query.data.value"><p class="path">当前目录：{{ query.data.value.root }}</p>
      <p v-if="!query.data.value.canMove" class="text-muted">当前目录由外部参数或 HTD_DATA_DIR 指定。移除覆盖后可在此迁移。</p>
      <template v-else><label>目标目录<input v-model="target" aria-label="目标数据目录" :disabled="busy || completed" @input="preview = undefined; pending = undefined" /></label><div class="settings-actions"><Button :disabled="busy || completed" @click="pick">选择文件夹</Button><Button :disabled="busy || completed || !target" @click="preflight">预检并迁移</Button></div></template>
      <p v-if="query.data.value.pendingTarget" class="path">上次迁移尚未完成：{{ query.data.value.pendingTarget }}。选择同一目标后可继续。</p><p v-if="query.data.value.canCleanup" class="path">旧副本：{{ query.data.value.previousRoot }}</p>
      <Button v-if="query.data.value.canCleanup" :disabled="busy" @click="cleanup">手动清理旧数据副本</Button>
    </template>
    <p v-if="busy" role="status">正在处理目录任务，请等待复制、校验和安全切换完成…</p><p v-if="completed" role="status">数据目录已切换，正在重启。若未自动重启，请重新打开应用。</p>
    <InlineAlert v-if="(error && !confirming) || query.error.value" severity="danger">{{ error || query.error.value?.message }}</InlineAlert>
    <ConfirmDialog :open="confirming" title="迁移并重启" :message="message" confirm-label="迁移并重启" :pending="busy" :error="error" @update:open="confirming = $event" @confirm="move" />
  </section>
</template>
<style scoped>label{display:grid;gap:8px}input{width:100%;min-width:0;padding:8px}.path{overflow-wrap:anywhere}</style>
