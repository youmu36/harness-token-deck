<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Vendor } from './types';
import { useCommand } from '../../shared/command';
import { asAppError } from '../../app/errors';
import { walletAmountInput } from '../../shared/money';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{vendor: Vendor}>();
const emit = defineEmits<{close: []; saved: []}>();
const command = useCommand('wallet.setLocalTotals');
const source = props.vendor.localWallet ?? props.vendor;
const decimal = walletAmountInput;
const fields = ref({C:decimal(source.C),pastTopUpPico:decimal(source.pastTopUpPico ?? '0'),pastConsumptionPico:decimal(source.pastConsumptionPico ?? '0')});
const labels = {C:'累计消费',pastTopUpPico:'过往充值',pastConsumptionPico:'过往消费'};
const originalFields = {...fields.value};
const balance = ref(decimal(source.B));
const calibrateBalance = ref(false);
const error = ref('');
function pico(value: string) {if(!/^-?[0-9]{1,26}(?:\.[0-9]{1,6})?$/.test(value))return;const [whole,fraction='']=value.replace('-','').split('.');return (BigInt(whole!)*10n**12n+BigInt(fraction.padEnd(12,'0')))*(value.startsWith('-')?-1n:1n);}
const valid = computed(() => Object.values(fields.value).every(v=>pico(v)!==undefined && pico(v)!>=0n) && (!calibrateBalance.value || pico(balance.value)!==undefined));
async function save() {
  if(!valid.value)return;
  try {await command.run({vendorId:props.vendor.id,expectedWalletRevision:props.vendor.walletRevision,...Object.fromEntries(Object.entries(fields.value).filter(([key,value])=>value !== originalFields[key as keyof typeof originalFields]).map(([key,value])=>[key,pico(value)!.toString()])),...(calibrateBalance.value?{B:pico(balance.value)!.toString()}:{})});emit('saved');emit('close');}
  catch(failure){error.value=asAppError(failure).message;}
}
</script>
<template>
  <Dialog :open="true" title="本地钱包校准与过往金额" :pending="command.pending.value" :error="error" @update:open="emit('close')">
    <form id="local-totals" class="fields" @submit.prevent="save">
      <p>{{vendor.name}} · 金额最多填写六位小数。累计充值由每笔“本次充值”累加；已有充值请在资金流水中校准。过往金额只计入总额。</p>
      <p>累计充值（只读）：{{decimal(source.R)}} USD</p>
      <label v-for="(_,key) in fields" :key="key">{{labels[key]}}（USD）<input v-model="fields[key]" :aria-label="`本地${labels[key]}`" inputmode="decimal" :disabled="command.pending.value" /></label>
      <label class="check"><input v-model="calibrateBalance" type="checkbox" />同时校准本地余额</label>
      <label v-if="calibrateBalance">目标余额（USD）<input v-model="balance" aria-label="本地目标余额" inputmode="decimal" /></label>
    </form>
    <template #footer><Button :disabled="command.pending.value" @click="emit('close')">取消</Button><Button type="submit" form="local-totals" variant="primary" :disabled="!valid" :pending="command.pending.value">保存校准</Button></template>
  </Dialog>
</template>
<style scoped>.fields{display:grid;gap:16px}label{display:grid;gap:6px}input:not([type=checkbox]){width:100%;padding:8px}.check{display:flex;align-items:center;gap:8px}p{font-size:13px;line-height:1.7}</style>
