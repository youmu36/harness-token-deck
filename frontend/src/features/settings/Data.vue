<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { useTask } from '../../shared/task';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import { useToast } from '../../ui/useToast';
import Button from '../../ui/Button.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const bridge = inject(BridgeKey)!;
const props = withDefaults(defineProps<{ mode?: TimeZoneMode }>(), { mode: 'system' });
const query = useQuery('maintenance.status', () => bridge.call('maintenance.status', {}).then(unwrap), { events: ['preferences.changed', 'sources.changed', 'task.done'] });
type Retention = RequestMap['maintenance.setRetention']['retention'];
const retention = ref<Retention>('forever'); const saving = ref(false); const error = ref(''); const confirming = ref(false);
const preview = ref<CommandData<'maintenance.previewRetention'>>();
const plan = ref<RequestMap['maintenance.setRetention']>(); const partial = ref(false);
watch(query.data, value => { if (value && !saving.value && !confirming.value) retention.value = value.retention; });
const confirmation = computed(() => preview.value ? `将把 ${preview.value.ledgerCount} 条到期用量发布为永久日汇总，再移除细粒度记录；将过期 ${preview.value.inboxCount} 条未入账记录；去重身份、价格与钱包记录保留。${preview.value.earliest ? `到期用量时间：${formatDateTime(preview.value.earliest, props.mode)} 至 ${formatDateTime(preview.value.latest!, props.mode)}。` : ''}` : '');
async function choose(value: string) {
  error.value = ''; saving.value = true; preview.value = undefined; partial.value = false;
  const next: Retention = value === 'forever' ? 'forever' : Number(value) as Retention;
  retention.value = next;
  try {
    const prefs = unwrap(await bridge.call('preferences.get', {}));
    plan.value = { retention: next, expectedRevision: prefs.revision, operationId: crypto.randomUUID() };
    const previous = query.data.value!.retention;
    if (next !== 'forever' && (previous === 'forever' || next < previous)) {
      preview.value = unwrap(await bridge.call('maintenance.previewRetention', { retention: next }));
      if (preview.value.ledgerCount || preview.value.inboxCount) { confirming.value = true; return; }
    }
    await commit();
  } catch (failure) { error.value = asAppError(failure).message; retention.value = query.data.value!.retention; }
  finally { saving.value = false; }
}
async function commit() {
  if (!plan.value) return; saving.value = true; error.value = '';
  try {
    const result = unwrap(await bridge.call('maintenance.setRetention', { ...plan.value }));
    confirming.value = false; partial.value = !result.complete; await query.refresh(); retention.value = result.retention;
    if (partial.value) error.value = `保留期限已保存，已处理 ${result.deletedLedger} 条细粒度用量、${result.expiredInbox} 条未入账记录；清理尚未完成（${result.errorCode}），可继续清理。`;
    else { plan.value = undefined; toast.success(`保留期限已保存，清理 ${result.deletedLedger + result.expiredInbox} 条记录`); }
  } catch (failure) {
    error.value = asAppError(failure).message; retention.value = query.data.value!.retention;
    if (asAppError(failure).code === 'PREFERENCES_REVISION_CONFLICT') { confirming.value = false; plan.value = undefined; preview.value = undefined; await query.refresh(); retention.value = query.data.value!.retention; error.value += '；请重新选择期限并预览确认。'; }
  } finally { saving.value = false; }
}
function cancelConfirm(value: boolean) { confirming.value = value; if (!value) { plan.value = undefined; preview.value = undefined; retention.value = query.data.value!.retention; error.value = ''; } }
const backup = useCommand('maintenance.backup'); const vacuum = useCommand('maintenance.vacuum'); const task = useTask(); const action = ref<'backup' | 'vacuum'>(); const toast = useToast();
async function start(kind: 'backup' | 'vacuum') { error.value = ''; action.value = kind; task.taskId.value = ''; try { task.taskId.value = (await (kind === 'backup' ? backup : vacuum).run({})).taskId; } catch (failure) { error.value = asAppError(failure).message; } }
async function open(kind: 'dataDir' | 'logDir') { error.value = ''; try { unwrap(await bridge.call('app.openPath', { kind })); } catch (failure) { error.value = asAppError(failure).message; } }
const mib = (value: number) => `${(value / 1048576).toFixed(2)} MiB`;
const busy = computed(() => saving.value || task.active.value || backup.pending.value || vacuum.pending.value);
watch(() => task.task.data.value?.status, value => { if (value === 'completed') { toast.success(action.value === 'backup' ? '数据库备份已完成' : '空闲空间已回收'); void query.refresh(); } });
</script>
<template><section class="settings-card" aria-label="数据"><h2>数据</h2><div v-if="query.data.value" class="settings-fields"><p>历史总量默认永久保存。以下期限只控制细粒度记录，发布并验证日汇总后才退出原贡献；账户资金、身份与审计继续保留。</p><label>保留期限<select aria-label="保留期限" :value="retention" :disabled="busy || confirming" @change="choose(($event.target as HTMLSelectElement).value)"><option value="90">90 天</option><option value="180">180 天</option><option value="360">360 天</option><option value="forever">永久</option></select></label><p>最近清理：{{ query.data.value.lastRetentionAt ? formatDateTime(query.data.value.lastRetentionAt, mode) : '尚未清理' }}</p><div v-if="query.data.value.storage" class="storage-details"><p>主库文件：{{ mib(query.data.value.storage.mainFileBytes) }} · WAL：{{ mib(query.data.value.storage.walBytes) }} · SHM：{{ mib(query.data.value.storage.shmBytes) }}</p><p>逻辑已分配：{{ mib(query.data.value.storage.allocatedBytes) }} · 已用页：{{ mib(query.data.value.storage.usedPageBytes) }} · 可回收整页：{{ mib(query.data.value.storage.freePageBytes) }}</p><p>备份：{{ mib(query.data.value.storage.backupBytes) }} · 迁移副本：{{ mib(query.data.value.storage.migrationBytes) }}</p><p class="text-muted">WAL 是可循环使用的日志空间；逻辑页已包含当前可见更新，不与 WAL 重复相加。</p><p class="text-muted">采样时间：{{ formatDateTime(query.data.value.storage.sampledAt, mode) }}</p></div><p v-else>数据库文件合计：{{ mib(query.data.value.databaseBytes) }}</p><p class="path">数据目录：{{ query.data.value.dataDir }}</p></div><div class="settings-actions"><Button :disabled="!query.data.value || busy" @click="start('vacuum')">回收空闲空间</Button><Button :disabled="!query.data.value || busy" @click="start('backup')">备份数据库</Button><Button :disabled="!query.data.value" @click="open('dataDir')">打开文件夹</Button><Button v-if="partial && plan" :pending="saving" @click="commit">继续清理</Button><Button v-else-if="plan && error && !confirming" :pending="saving" @click="commit">重试保存与清理</Button></div><div v-if="task.taskId.value" role="status"><p v-if="task.active.value">{{ action === 'backup' ? '正在备份数据库…' : '正在回收空闲空间…' }}</p><p v-if="task.task.data.value?.status === 'completed'">{{ action === 'backup' ? '备份已完成' : '空闲空间已回收' }}<span v-if="task.task.data.value.result?.path" class="path">：{{ task.task.data.value.result.path }}</span></p></div><InlineAlert v-if="(error && !confirming) || query.error.value || task.task.error.value || task.task.data.value?.error" severity="danger">{{ error || query.error.value?.message || task.task.error.value?.message || task.task.data.value?.error?.message }}</InlineAlert><ConfirmDialog :open="confirming" title="缩短保留期限" :message="confirmation" confirm-label="保存并清理" :pending="saving" :error="error" @update:open="cancelConfirm" @confirm="commit" /></section></template>
