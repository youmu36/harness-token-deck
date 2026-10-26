<script setup lang="ts">
import { computed, inject, onScopeDispose, ref, shallowRef, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { formatDateTimeInput, parseDateTimeInput, type TimeZoneMode } from '../../shared/time';
import { useToast } from '../../ui/useToast';
import type { Harness } from '../harness/types';
import Dialog from '../../ui/Dialog.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import DateTimeField from '../../ui/DateTimeField.vue';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
const props = withDefaults(defineProps<{ harness: Harness; mode?: TimeZoneMode }>(), { mode: 'system' });
const emit = defineEmits<{ close: []; committed: [] }>();
const bridge = inject(BridgeKey)!; const toast = useToast();
const open = ref(true); const confirming = ref(false); const error = ref(''); const previewing = ref(false);
const now = new Date();
const defaultStart = new Date(now.getTime() - 72 * 3600000 + 5 * 60000);
const start = ref(formatDateTimeInput(defaultStart, props.mode));
const end = ref(formatDateTimeInput(now, props.mode));
const minimum = computed(() => formatDateTimeInput(new Date(now.getTime() - 72 * 3600000), props.mode));
const maximum = computed(() => formatDateTimeInput(new Date(now.getTime() + 60000), props.mode));
const startPristine = ref(true); const endPristine = ref(true);
function editStart(value: string) { if (start.value !== value) startPristine.value = false; start.value = value; }
function editEnd(value: string) { if (end.value !== value) endPristine.value = false; end.value = value; }
const groupId = ref('');
const startValid = ref(true); const endValid = ref(true); const target = ref('');
const catalog = useQuery('recalc.catalog', () => bridge.call('pricing.catalog', {}).then(unwrap), { events: ['pricing.changed'] });
const versions = computed(() => catalog.data.value?.groups.flatMap(group => group.versions.filter(version => !version.disabled).map(version => ({ groupId: group.id, versionId: version.id, label: `${version.displayName}${group.disabled ? '（已停用）' : ''}` }))) ?? []);
watch(catalog.data, value => {
  if (!target.value && value) {
    const current = value.current[props.harness]; const group = value.groups.find(g => g.id === current?.groupId);
    groupId.value = group?.id ?? '';
    target.value = group?.versions.find(v => v.current)?.id ?? current?.versionId ?? '';
  }
});
const visibleVersions = computed(() => versions.value.filter(v => v.groupId === groupId.value));
watch(groupId, () => { if (!visibleVersions.value.some(v => v.versionId === target.value)) target.value = visibleVersions.value[0]?.versionId ?? ''; });
const preview = shallowRef<CommandData<'maintenance.previewRecalc'>>();
const plan = shallowRef<RequestMap['maintenance.previewRecalc']>();
const commit = useCommand('maintenance.commitRecalc');
let generation = 0; let disposed = false;
function invalidate() { ++generation; preview.value = undefined; plan.value = undefined; confirming.value = false; previewing.value = false; error.value = ''; }
watch([start, end, target, startValid, endValid, () => props.mode], invalidate, { flush: 'sync' });
// The server remains authoritative; clear the displayed plan when notified of a newer snapshot.
const unsubscribe = ['pricing.changed', 'usage.changed', 'wallet.changed'].map(name => bridge.events.subscribe(name as 'pricing.changed' | 'usage.changed' | 'wallet.changed', () => {
  // A timed-out commit may have succeeded; preserve its receipt key even if its events arrive later.
  if (!commit.pending.value && commit.error.value?.code !== 'WRITE_TIMEOUT') invalidate();
}));
onScopeDispose(() => { disposed = true; ++generation; unsubscribe.forEach(fn => fn()); });
function parameters(): RequestMap['maintenance.previewRecalc'] {
  const version = versions.value.find(v => v.versionId === target.value);
  if (!version) throw new RangeError('请选择目标价格版本');
  if (!startValid.value || !endValid.value) throw new RangeError('请检查日期与时间');
  // Preserve the default "now" down to milliseconds, including requests in the current minute.
  const from = startPristine.value ? defaultStart : parseDateTimeInput(start.value, props.mode);
  const to = endPristine.value ? now : parseDateTimeInput(end.value, props.mode);
  const current = Date.now();
  if (to <= from) throw new RangeError('结束时间须晚于开始时间');
  if (to.getTime() - from.getTime() > 72 * 3600000) throw new RangeError('重算范围不能超过 72 小时');
  if (from.getTime() < current - 72 * 3600000) throw new RangeError('所选记录已超出允许重算的保留范围');
  if (to.getTime() > current + 60000) throw new RangeError('重算范围不能包含未来时间');
  return { harness: props.harness, start: from.toISOString(), end: to.toISOString(), groupId: version.groupId, versionId: version.versionId };
}
async function showPreview() {
  invalidate(); const current = generation; previewing.value = true;
  try {
    const input = parameters(); const result = unwrap(await bridge.call('maintenance.previewRecalc', input));
    if (!disposed && current === generation) { plan.value = input; preview.value = result; }
  } catch (failure) { if (!disposed && current === generation) error.value = failure instanceof RangeError ? failure.message : asAppError(failure).message; }
  finally { if (!disposed && current === generation) previewing.value = false; }
}
async function save() {
  if (!plan.value || !preview.value) return;
  error.value = '';
  try {
    await commit.run({ ...plan.value, fingerprint: preview.value.fingerprint });
    toast.success('历史重算完成，统计与钱包已更新'); emit('committed'); confirming.value = false; open.value = false;
  } catch (failure) {
    const cause = asAppError(failure);
    if (cause.code === 'RECALC_PLAN_CHANGED') { invalidate(); error.value = cause.message; }
    else error.value = cause.message;
  }
}
const delta = computed(() => preview.value ? (BigInt(preview.value.newTotal) - BigInt(preview.value.oldTotal)).toString() : '0');
const disabled = computed(() => previewing.value || commit.pending.value);
watch(open, value => { if (!value) emit('close'); });
</script>
<template>
  <Dialog v-model:open="open" title="重算历史" :pending="disabled || confirming" :error="confirming ? '' : error || catalog.error.value?.message">
    <p>使用目标版本预览时的最新修订，按每条请求原完成时间和总输入重新判断峰谷与长上下文规则。范围仅限最近 72 小时；保留原供应商，已锁定的人工金额保持不变。重算不可撤销。</p>
    <div class="recalc-fields"><div class="recalc-time"><span>开始时间 · {{ mode === 'UTC' ? 'UTC' : '本机时区' }}</span><DateTimeField :model-value="start" :min="minimum" :max="maximum" label="重算开始时间" :mode="mode" :disabled="commit.pending.value || confirming" @update:model-value="editStart" @validity-change="startValid = $event; if (!$event) startPristine = false" /></div><div class="recalc-time"><span>结束时间</span><DateTimeField :model-value="end" :min="minimum" :max="maximum" label="重算结束时间" :mode="mode" :disabled="commit.pending.value || confirming" @update:model-value="editEnd" @validity-change="endValid = $event; if (!$event) endPristine = false" /></div><div class="recalc-target"><label>价格分组<select v-model="groupId" aria-label="重算价格分组" :disabled="disabled || confirming"><option value="" disabled>选择价格分组</option><option v-for="group in catalog.data.value?.groups" :key="group.id" :value="group.id">{{ group.name }}</option></select></label><label>倍率版本<select v-model="target" aria-label="重算目标版本" :disabled="commit.pending.value || confirming || catalog.loading.value"><option value="" disabled>选择价格版本</option><option v-for="version in visibleVersions" :key="version.versionId" :value="version.versionId">{{ version.label }}</option></select></label></div></div>
    <Button v-if="catalog.error.value" @click="catalog.refresh">重新读取价格</Button>
    <div v-if="preview" class="recalc-preview" aria-label="重算预览"><p>{{ preview.groupName }} · {{ preview.versionDisplayName }} · {{ preview.recordCount }} 条记录</p><p>范围内 {{ preview.totalCount }} 条，可重算 {{ preview.recordCount }} 条；缺少明细 {{ preview.unavailableCount }} 条（其中待计费 {{ preview.pendingCount }} 条），证据不足或不完整 {{ preview.evidenceInsufficientCount }} 条。</p><p>目标修订 #{{ preview.targetRevisionSeq }}；长上下文命中 {{ preview.ruleCounts.tier }} 条，峰谷命中 {{ preview.ruleCounts.peak }} 条。阈值采用单请求总输入，已包含缓存；来源容量或累计值不参与判断。</p><dl><div><dt>原成本</dt><dd><Money :pico="preview.oldTotal" mode="full6" /></dd></div><div><dt>新成本</dt><dd><Money :pico="preview.newTotal" mode="full6" /></dd></div><div><dt>成本差额</dt><dd><Money :pico="delta" mode="full6" /></dd></div><div><dt>未定价 / 不完整</dt><dd>{{ preview.unpricedCount }} / {{ preview.incompleteCount }}</dd></div></dl><h3>受影响供应商</h3><p v-if="!preview.vendors.length">没有可记账的供应商归属，钱包不变。</p><ul v-else><li v-for="vendor in preview.vendors" :key="vendor.vendorId"><strong>{{ vendor.name }}</strong><span>本地消费变化 <Money :pico="vendor.consumptionDelta" mode="full6" /></span><span>本地余额投影变化 <Money :pico="vendor.balanceDelta" mode="full6" /></span></li></ul></div>
    <template #footer><Button :disabled="disabled || confirming" @click="open = false">取消</Button><Button :pending="previewing" :disabled="disabled || confirming || !target || !startValid || !endValid" @click="showPreview">预览</Button><Button variant="primary" :disabled="disabled || confirming || !preview || !preview.recordCount" @click="confirming = true">确认重算</Button></template>
  </Dialog>
  <ConfirmDialog v-model:open="confirming" title="确认重算历史" :message="`将重算 ${preview?.recordCount ?? 0} 条记录，并更新本地费用和钱包差额；已锁定的人工金额保持不变。此操作不可撤销。`" confirm-label="执行重算" :pending="commit.pending.value" :error="error" @confirm="save" />
</template>
<style scoped>.recalc-target { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.recalc-fields { display: grid; gap: 16px; margin-block: 20px; }.recalc-fields label, .recalc-time { display: grid; gap: 8px; }.recalc-fields select { max-width: 100%; min-width: 0; }.recalc-preview { display: grid; gap: 16px; }dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }dt { color: var(--text-muted); }dd { margin: 4px 0 0; overflow-wrap: anywhere; }h3 { font-size: 14px; }ul { padding: 0; list-style: none; display: grid; gap: 12px; }li { display: flex; flex-wrap: wrap; gap: 8px 16px; overflow-wrap: anywhere; }</style>
