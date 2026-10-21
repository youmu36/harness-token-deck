<script setup lang="ts">
import { computed, ref } from 'vue';
import type { CommandData } from '../../app/ports';
import { useCommand } from '../../shared/command';
import { walletAmountInput } from '../../shared/money';
import { asAppError } from '../../app/errors';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
const props = defineProps<{vendorId:string;scope:'local'|'remote';entry:CommandData<'wallet.accountEntries'>['entries'][number]}>();
const emit = defineEmits<{close:[];saved:[]}>();
const command = useCommand('wallet.correctEntry');
const original = props.entry.kind === 'top_up' ? props.entry.R : props.entry.C;
const initial = walletAmountInput(original);
const amount = ref(initial);
const note = ref(props.entry.note);
const error = ref('');
const pico = computed(() => {
  if (amount.value === initial) return original;
  if (!/^[0-9]{1,26}(?:\.[0-9]{1,6})?$/.test(amount.value)) return;
  const [whole, fraction=''] = amount.value.split('.');
  return (BigInt(whole!)*10n**12n+BigInt(fraction.padEnd(12,'0'))).toString();
});
async function save() {
  if (pico.value === undefined || command.pending.value) return;
  try {
    await command.run({vendorId:props.vendorId,scope:props.scope,entryId:props.entry.id,
      R:props.entry.kind === 'top_up' ? pico.value : '0',
      C:props.entry.kind === 'consumption' ? pico.value : '0',
      expectedEntryRevision:props.entry.entryRevision ?? 1,note:note.value});
    emit('saved'); emit('close');
  } catch (failure) {error.value=asAppError(failure).message;}
}
</script>
<template>
  <Dialog :open="true" :title="entry.kind === 'top_up' ? '校准充值流水' : '校准消费流水'" :pending="command.pending.value" :error="error" @update:open="emit('close')">
    <form id="flow-correction" class="fields" @submit.prevent="save">
      <p>原金额：<Money :pico="original" />。校准后更新当前钱包的累计金额，并修订原日期的记账。</p>
      <p class="text-muted">{{scope === 'local' ? '本地余额随充值或消费差额变化。' : '远程余额保持最近观测值；这里只纠正充值或消费的记账金额。'}} 校准记录会在本地数据静默十分钟后出现在流水中。</p>
      <p v-if="scope==='local' && entry.kind==='consumption'" class="text-muted">人工校准后的金额会保留。后续重算保留人工金额，并以调整流水说明估算变化；如有差额会提示核对，提醒可单独关闭。</p>
      <label>校准后的金额（USD）<input v-model="amount" aria-label="流水校准金额" inputmode="decimal" :disabled="command.pending.value" /></label>
      <label>备注<textarea v-model="note" aria-label="流水校准备注" maxlength="500" :disabled="command.pending.value" /></label>
    </form>
    <template #footer><Button :disabled="command.pending.value" @click="emit('close')">取消</Button><Button form="flow-correction" type="submit" variant="primary" :pending="command.pending.value" :disabled="pico === undefined">保存校准</Button></template>
  </Dialog>
</template>
<style scoped>.fields{display:grid;gap:16px}label{display:grid;gap:6px}input,textarea{width:100%;padding:8px}textarea{min-height:80px;resize:vertical}p{font-size:13px;line-height:1.7}</style>
