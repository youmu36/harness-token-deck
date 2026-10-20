<script setup lang="ts">
import { computed, ref, shallowRef } from 'vue';
import { type CommandData } from '../../app/ports';
import { asAppError } from '../../app/errors';
import { useCommand } from '../../shared/command';
import { walletAmountInput } from '../../shared/money';
import { type Vendor } from './types';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import AccountEntryPicker from './AccountEntryPicker.vue';
import AccountEntryDetails from './AccountEntryDetails.vue';
import type { TimeZoneMode } from '../../shared/time';
const props = withDefaults(defineProps<{ vendor: Vendor; target: 'top_up' | 'consumption'; mode?: TimeZoneMode; scope?: 'local'|'remote' }>(), {mode:'system'});
const emit = defineEmits<{ close: []; saved: [] }>();
const classification = useCommand('wallet.reclassify');
const consumption = useCommand('wallet.correctConsumption');
type Entry = CommandData<'wallet.accountEntries'>['entries'][number];
const source = shallowRef<Entry>();
const picking = ref(false);
const revision = ref(props.scope === 'local' ? props.vendor.walletRevision : props.vendor.account?.revision ?? 0);
const amount = ref('');
const direct = ref(props.scope === 'remote'); const note = ref(props.target === 'top_up' ? '人工调整类型 校准→充值' : '人工调整类型 校准→消费');
const target = ref<'top_up' | 'consumption' | 'adjustment'>(props.target);
const error = ref('');
const pending = computed(() => classification.pending.value || consumption.pending.value);
const available = computed(() => source.value?.availablePico ?? '0');
function select(row: Entry, accountRevision: number) {
  source.value = row; revision.value = accountRevision; picking.value = false;
  amount.value = walletAmountInput(row.availablePico, true);
  error.value = '';
}
async function save() {
  if (!valid.value || pending.value) return;
  error.value = '';
  try {
    if (direct.value) await consumption.run({ vendorId: props.vendor.id, ...(props.scope === 'remote' ? {scope:'remote' as const} : {}), targetPico: amountPico(amount.value)!.toString(), note: note.value, expectedAccountRevision: revision.value });
    else await classification.run({ vendorId: props.vendor.id, ...(props.scope ? {scope:props.scope} : {}), entryId: source.value!.id, amountPico: amountPico(amount.value)!.toString(), targetKind: target.value, note: note.value, expectedAccountRevision: revision.value });
    emit('saved'); emit('close');
  } catch (failure) {
    error.value = asAppError(failure).message;
    if (asAppError(failure).code === 'ACCOUNT_REVISION_CONFLICT') source.value = undefined;
  }
}
function amountPico(value: string): bigint | undefined { if(!/^[0-9]{1,27}(?:\.[0-9]{1,6})?$/.test(value))return;const [whole,fraction='']=value.split('.');return BigInt(whole!)*1_000_000_000_000n+BigInt(fraction.padEnd(12,'0')); }
const valid = computed(() => amountPico(amount.value) !== undefined && (direct.value ? amountPico(amount.value)! >= 0n : !!source.value && amountPico(amount.value)! > 0n && amountPico(amount.value)! <= BigInt(available.value)));
</script>
<template>
  <Dialog :open="true" :title="target === 'top_up' ? '人工校准累计充值' : '人工校准累计消费'" :error="error" :pending="pending" @update:open="emit('close')">
    <form id="account-adjustment" class="fields" @submit.prevent="save">
      <p>{{ vendor.name }} · 原流水保留，追加更正记录；当前余额保持不变。</p>
      <label v-if="props.target === 'consumption' && !scope" class="check"><input v-model="direct" type="checkbox" />直接指定累计消费</label>
      <template v-if="!direct">
        <div class="source-entry"><AccountEntryDetails v-if="source" :entry="source" :mode="mode" /><p v-else class="text-muted">尚未选择原资金流水</p><Button aria-label="选择原流水" :disabled="pending" @click="picking = true">{{ source ? '重新选择流水' : '选择原流水' }}</Button></div>
        <label>改为<select v-model="target" aria-label="改判类型"><option value="top_up">充值</option><option value="consumption">消费</option><option value="adjustment">调整与校准</option></select></label>
      </template>
      <label>{{ direct ? '校准后的累计消费（USD）' : '改判金额（USD）' }}<input v-model="amount" aria-label="校准金额" inputmode="decimal" /></label>
      <label>备注<input v-model="note" aria-label="校准备注" maxlength="500" /></label>
      <p class="text-muted">充值包含区间净流入、赠送和退款，不代表现金支付或真实充值次数。改判后仍保留已匹配资金，自动核对不会再次补入。</p>
    </form>
    <template #footer><Button :disabled="pending" @click="emit('close')">取消</Button><Button type="submit" form="account-adjustment" variant="primary" :pending="pending" :disabled="!valid">保存更正</Button></template>
  </Dialog>
  <AccountEntryPicker v-if="picking" :vendor-id="vendor.id" :scope="scope" :mode="mode" @close="picking = false" @select="select" />
</template>
<style scoped>.fields { display:grid;gap:16px; }.source-entry{display:grid;gap:12px;padding:14px;border:1px solid var(--border);border-radius:6px}label { display:grid;gap:6px; }input, select { padding:8px;min-width:0; }.check { display:flex;align-items:center;gap:8px; }p { font-size:13px; }</style>
