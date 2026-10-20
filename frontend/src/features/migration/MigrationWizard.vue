<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { ArrowRight, Check, DatabaseBackup, FolderOpen, RefreshCw } from 'lucide-vue-next';
import { BridgeKey, type CommandData } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { useTask } from '../../shared/task';
import { migrationResult } from './types';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Money from '../../ui/Money.vue';

const emit = defineEmits<{ close: []; activated: [] }>();
const bridge = inject(BridgeKey)!;
const open = ref(true); const error = ref(''); const reportPath = ref('');
const sourcePath = ref(''); const picking = ref(false);
const sourceRequest = computed(() => sourcePath.value.trim() ? { sourcePath: sourcePath.value.trim() } : {});
const detection = useQuery('migration.detect', () => bridge.call('migration.detect', sourceRequest.value).then(unwrap), { deps: [sourcePath], clearOnChange: true, events: ['usage.changed', 'wallet.changed'] });
const run = useCommand('migration.run'); const activation = useCommand('migration.activate'); const task = useTask();
const result = computed(() => task.task.data.value?.status === 'completed' ? migrationResult(task.task.data.value.result) : undefined);
const confirmed = ref(false); const activated = ref<CommandData<'migration.activate'>>();
const busy = computed(() => picking.value || run.pending.value || task.active.value || activation.pending.value);
const phase = computed(() => activated.value ? 4 : confirmed.value ? 3 : result.value ? 2 : task.taskId.value ? 1 : 0);
const steps = ['检测旧库', '快照与校验', '报告', '确认激活', '完成'];
const counts = computed(() => result.value?.counts?.filter(row => ['ledger', 'identity', 'checkpoint', 'recalc_batch'].includes(row.table)) ?? []);
const labels: Record<string, string> = { ledger: '账本', identity: '去重身份', checkpoint: '可解析检查点', recalc_batch: '重算批次' };
async function start() {
  error.value = ''; confirmed.value = false; task.taskId.value = '';
  try {
    const started = await run.run({ dryRun: true, ...sourceRequest.value });
    reportPath.value = started.reportPath; task.taskId.value = started.taskId;
  } catch (failure) { error.value = asAppError(failure).message; }
}
async function pickSource() {
  error.value = ''; picking.value = true;
  try {
    const chosen = unwrap(await bridge.call('app.pickPath', { kind: 'file', initial: sourcePath.value.trim() || detection.data.value?.sourcePath }));
    if (chosen.path) sourcePath.value = chosen.path;
  } catch (failure) { error.value = asAppError(failure).message; }
  finally { picking.value = false; }
}
function changeSource() { task.taskId.value = ''; reportPath.value = ''; error.value = ''; confirmed.value = false; }
async function activate() {
  if (!result.value?.ok) return;
  error.value = '';
  try { activated.value = await activation.run({ migrationId: result.value.migrationId }); emit('activated'); }
  catch (failure) { error.value = asAppError(failure).message; }
}
async function cancel() { try { await task.cancel(); } catch (failure) { error.value = asAppError(failure).message; } }
watch(open, value => { if (!value) emit('close'); });
</script>

<template>
  <Dialog v-model:open="open" title="导入旧版数据" :pending="busy" :error="error || detection.error.value?.message || task.task.error.value?.message || task.task.data.value?.error?.message">
    <ol class="migration-steps" aria-label="迁移步骤"><li v-for="(step, index) in steps" :key="step" :aria-current="phase === index ? 'step' : undefined"><Check v-if="phase > index" :size="14" aria-hidden="true" /><span v-else>{{ index + 1 }}</span>{{ step }}</li></ol>
    <div v-if="!task.taskId.value && !activated" class="source-picker">
      <label for="legacy-source-path">数据库或备份路径</label>
      <div class="source-input"><input id="legacy-source-path" v-model.trim="sourcePath" :placeholder="detection.data.value?.sourcePath || '留空检测默认位置，或输入完整文件路径'" :disabled="busy" maxlength="32767" /><Button :disabled="busy" :pending="picking" @click="pickSource"><FolderOpen :size="16" />选择文件…</Button></div>
      <p class="text-muted">可选择任意目录中的旧版数据库或数据库备份；留空使用默认位置。也支持本软件的账本备份，系统会自动识别格式；目标账本须为空。</p>
    </div>
    <template v-if="activated">
      <InlineAlert severity="success">旧版数据已激活，账本与钱包已刷新。</InlineAlert>
      <p>旧库保留位置</p><p class="path">{{ activated.sourcePath }}</p>
      <p>回退时先退出新版，再运行旧版。新版期间新增的数据不会出现在旧版。</p>
    </template>
    <template v-else-if="confirmed">
      <h3>激活这份已校验的快照</h3>
      <p>旧库将原样保留。激活后由新版继续采集。</p>
      <InlineAlert severity="warning">新版数据不会同步回旧版。回退时必须先退出新版，再运行旧版。</InlineAlert>
      <p class="path">快照：{{ result?.snapshotPath }}</p>
    </template>
    <template v-else-if="result">
      <InlineAlert :severity="result.ok ? 'success' : 'danger'">{{ result.ok ? '全部校验通过，尚未激活。' : '迁移校验失败，不能激活。' }}</InlineAlert>
      <div v-if="result.ok" class="migration-summary">
        <p>来源版本 {{ result.sourceVersion }}</p>
        <dl class="counts"><div v-for="row in counts" :key="row.table"><dt>{{ labels[row.table] }}</dt><dd>{{ row.old }} → {{ row.new }}</dd></div></dl>
        <p>账本总额 <Money :pico="result.oldTotal!" /> → <Money :pico="result.newTotal!" /></p>
        <div class="table-scroll"><table><caption>供应商余额对照</caption><thead><tr><th>供应商</th><th>B_old</th><th>B_new</th></tr></thead><tbody><tr v-for="(vendor, index) in result.vendors" :key="index"><td>{{ vendor.name }}</td><td><Money :pico="vendor.old" /></td><td><Money :pico="vendor.new" /></td></tr></tbody></table></div>
      </div>
      <ul v-if="result.errors.length"><li v-for="(message, index) in result.errors" :key="index">{{ message }}</li></ul>
      <details v-if="result.warnings.length"><summary>警告（{{ result.warnings.length }}）</summary><ul><li v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</li></ul></details>
      <p v-else class="text-muted">警告 0 项</p>
    </template>
    <template v-else-if="task.taskId.value">
      <p v-if="task.active.value" role="status">{{ (task.task.data.value?.completed ?? 0) === 0 ? '正在生成旧库快照…' : (task.task.data.value?.completed ?? 0) === 1 ? '正在转换到临时新库…' : '正在逐项校验…' }}</p>
      <progress v-if="task.active.value" :value="task.task.data.value?.completed ?? 0" :max="3" />
      <p v-if="task.task.data.value?.status === 'cancelled'" role="status">演练已取消，活动库未改变。</p>
      <InlineAlert v-if="task.task.data.value?.status === 'completed' && !result" severity="danger">迁移结果格式无效，请查看报告后重新演练。</InlineAlert>
    </template>
    <template v-else-if="detection.data.value">
      <p>{{ detection.data.value.detected ? '检测到旧版数据库' : '未发现旧版数据库' }}</p>
      <p class="path">{{ detection.data.value.sourcePath }}</p>
      <InlineAlert v-if="detection.data.value.legacyRunning" severity="warning">请先退出旧版 Harness Token Usage，再重新检测。</InlineAlert>
      <InlineAlert v-else-if="!detection.data.value.targetEmpty" severity="warning">{{ detection.data.value.reason }}</InlineAlert>
      <p>生成只读快照并导入临时新库，校验通过后查看报告，再确认激活。</p>
    </template>
    <p v-else role="status">正在检测旧库…</p>
    <p v-if="reportPath" class="path report-path">报告路径：{{ reportPath }}</p>
    <template #footer>
      <Button :disabled="busy" @click="open = false">{{ activated ? '完成' : '关闭' }}</Button>
      <Button v-if="task.active.value" :disabled="task.cancelling.value || task.task.data.value?.cancelRequested" @click="cancel">取消演练</Button>
      <template v-else-if="!activated">
        <Button v-if="task.taskId.value && !task.active.value && !confirmed" :disabled="busy" @click="changeSource">更换旧库</Button>
        <Button v-if="confirmed" :disabled="busy" @click="confirmed = false">返回报告</Button>
        <Button v-if="confirmed" variant="primary" :pending="activation.pending.value" @click="activate"><ArrowRight :size="16" />确认激活</Button>
        <Button v-else-if="result?.ok" variant="primary" @click="confirmed = true"><ArrowRight :size="16" />继续</Button>
        <template v-else>
          <Button v-if="!task.taskId.value" :disabled="busy || detection.loading.value || detection.refreshing.value" @click="detection.refresh()"><RefreshCw :size="16" />重新检测</Button>
          <Button variant="primary" :disabled="busy || (!task.taskId.value && (detection.loading.value || detection.refreshing.value || !detection.data.value?.canMigrate))" @click="start"><DatabaseBackup :size="16" />{{ task.taskId.value ? '重新演练' : '生成快照并演练' }}</Button>
        </template>
      </template>
    </template>
  </Dialog>
</template>

<style scoped>
.button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; white-space: nowrap; }
.source-picker { display: grid; gap: 8px; margin-bottom: 20px; }.source-input { display: flex; gap: 8px; }.source-input input { flex: 1; min-width: 0; }.source-picker p { font-size: 12px; }
.migration-steps { display: flex; justify-content: space-between; gap: 8px; list-style: none; padding: 0; margin-bottom: 24px; font-size: 11px; color: var(--text-muted); }
.migration-steps li { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
.migration-steps li[aria-current] { color: var(--accent); font-weight: 600; }
.migration-summary, .counts { display: grid; gap: 12px; margin-block: 16px; }
.counts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.counts dt { color: var(--text-muted); font-size: 12px; }.counts dd { margin: 4px 0 0; font-variant-numeric: tabular-nums; }
.path, li { overflow-wrap: anywhere; }.report-path { margin-top: 16px; font-size: 12px; }
progress { width: 100%; height: 12px; margin-block: 16px; }
h3 { font-size: 16px; margin-bottom: 12px; }p + p, p + .inline-alert, .inline-alert + p { margin-top: 12px; }
.table-scroll { overflow-x: auto; }table { width: 100%; border-collapse: collapse; font-size: 12px; }caption { text-align: left; font-weight: 600; margin-bottom: 8px; }
th, td { padding: 8px 4px; border-bottom: 1px solid var(--border); text-align: left; }th:not(:first-child), td:not(:first-child) { text-align: right; white-space: nowrap; }
td:first-child { overflow-wrap: anywhere; }details { margin-block: 16px; }summary { cursor: pointer; }ul { padding-left: 20px; margin-top: 8px; }
</style>
