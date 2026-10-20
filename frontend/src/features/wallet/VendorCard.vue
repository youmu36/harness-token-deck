<script setup lang="ts">
import { computed } from 'vue';
import type { Vendor, WalletAction } from './types';
import type { Catalog } from '../harness/types';
import type { TimeZoneMode } from '../../shared/time';
import { formatDateTime } from '../../shared/format';
import Money from '../../ui/Money.vue';
import Badge from '../../ui/Badge.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ vendor: Vendor; catalog?: Catalog; mode: TimeZoneMode; scope?: 'local' | 'remote' }>();
const emit = defineEmits<{ action: [action: WalletAction]; entries: []; correct: [target: 'top_up' | 'consumption']; local: [] }>();
const amounts = computed(() => props.scope === 'local' ? (props.vendor.localWallet ?? props.vendor) : (props.vendor.remoteWallet ?? props.vendor.account ?? props.vendor));
const pastR = computed(() => 'pastTopUpPico' in amounts.value ? amounts.value.pastTopUpPico ?? '0' : props.vendor.pastTopUpPico ?? '0');
const pastC = computed(() => 'pastConsumptionPico' in amounts.value ? amounts.value.pastConsumptionPico ?? '0' : props.vendor.pastConsumptionPico ?? '0');
const postedR = computed(() => 'postedR' in amounts.value ? amounts.value.postedR ?? amounts.value.R : amounts.value.R);
const postedC = computed(() => 'postedC' in amounts.value ? amounts.value.postedC ?? amounts.value.C : amounts.value.C);
const pendingR = computed(() => 'pendingTopUpPico' in amounts.value ? amounts.value.pendingTopUpPico : '0');
const pendingC = computed(() => 'pendingConsumptionPico' in amounts.value ? amounts.value.pendingConsumptionPico : '0');
const totalR = computed(() => (BigInt(postedR.value)+BigInt(pastR.value)).toString());
const totalC = computed(() => (BigInt(postedC.value)+BigInt(pastC.value)).toString());
function action(value: WalletAction) { emit('action', value); }
</script>
<template>
  <article class="vendor-card surface">
    <header><h2>{{scope === 'local' ? '本地钱包' : '远程钱包'}}概览</h2><Badge v-if="vendor.status === 'archived'">已归档</Badge><Badge v-else>{{scope === 'local' ? '本地估算' : '远程账户'}}</Badge></header>
    <div class="balance"><span class="text-muted">当前余额</span><Money :pico="amounts.B" mode="usd4" /></div>
    <dl>
      <div class="with-pending"><dt>累计充值<span>待更新金额</span></dt><dd><Money v-if="scope === 'local'" :pico="postedR" mode="usd4" /><button v-else class="amount-link" :disabled="vendor.status === 'archived'" @click="emit('correct','top_up')"><Money :pico="postedR" mode="usd4" /></button><Money class="pending-amount" :pico="pendingR" mode="usd4" aria-label="待更新充值金额" /></dd></div>
      <div><dt>过往充值</dt><dd><Money :pico="pastR" mode="usd4" /></dd></div><div class="total"><dt>充值总额</dt><dd><Money :pico="totalR" mode="usd4" /></dd></div>
      <div class="with-pending"><dt>累计消费<span>待更新金额</span></dt><dd><button class="amount-link" :disabled="vendor.status === 'archived'" @click="scope === 'local' ? emit('local') : emit('correct','consumption')"><Money :pico="postedC" mode="usd4" /></button><Money class="pending-amount" :pico="pendingC" mode="usd4" aria-label="待更新消费金额" /></dd></div>
      <div><dt>过往消费</dt><dd><Money :pico="pastC" mode="usd4" /></dd></div><div class="total"><dt>消费总额</dt><dd><Money :pico="totalC" mode="usd4" /></dd></div>
    </dl>
    <p class="text-muted">累计与总额显示已生成流水的金额；待更新金额在静默结算后归入累计并清零，远程待更新金额结算前可能随观测修正。</p>
    <p v-if="vendor.coverageIncomplete && scope === 'local'"><Badge tone="warning">累计消费不完整</Badge><span class="quality text-muted">未定价 {{ vendor.unpricedCount }} 条，成本不完整 {{ vendor.incompleteCount }} 条</span></p>
    <p v-if="vendor.historyCoverageStart" class="text-muted">历史覆盖自 {{ formatDateTime(vendor.historyCoverageStart, mode) }}</p>
    <p v-if="scope !== 'remote' && vendor.lastCalibratedAt" class="text-muted">最近校准：{{ formatDateTime(vendor.lastCalibratedAt, mode) }}</p>
    <p v-if="scope !== 'local' && vendor.account?.observedAt" class="text-muted">远程余额观测于 {{ formatDateTime(vendor.account.observedAt, mode) }}</p>
    <p v-if="scope !== 'local' && vendor.account" class="text-muted">本地估算成本：<Money :pico="vendor.account.localEstimatePico" /> · {{ vendor.account.methods.map(method => ({remote_usage: '供应商累计核算', balance_estimate: '余额推算', balance_unresolved: '推算 / 差额待核对', legacy_local: '旧口径本地账', local_estimate: '本地估算'}[method] ?? method)).join('、') }}</p>
    <p class="text-muted wallet-note">{{scope === 'local' ? '累计充值来自每次人工充值，累计消费来自本地估算。过往金额用于记录接入前的历史，只计入总额。' : '接入后的累计金额根据远程余额变化核算。只有余额时参考同期本地估算；本地钱包的手动金额不参与远程记账。'}}</p>
    <footer v-if="vendor.status === 'active'"><template v-if="scope === 'local'"><Button variant="primary" @click="action('topup')">本次充值</Button><Button @click="emit('local')">校准与过往金额</Button></template><Button v-else @click="action('calibrate')">校准远程余额</Button><Button @click="emit('entries')">查看资金流水</Button></footer><footer v-else><Button @click="action('restore')">恢复供应商</Button><Button @click="emit('entries')">查看流水</Button></footer>
  </article>
</template>
<style scoped>
.with-pending dt,.with-pending dd{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;align-items:baseline}.with-pending dt span,.pending-amount{text-align:right}.with-pending dd>.amount-link{text-align:left;min-width:0}.pending-amount{color:var(--text-muted)}@media(max-width:850px){.vendor-card dl{grid-template-columns:minmax(0,1fr)}}
.amount-link { padding:0;border:0;background:transparent;text-decoration:underline;text-underline-offset:3px; }
.vendor-card { padding: 18px; display: grid; gap: 14px; min-width: 0; border-radius: 8px; }header, footer { display: flex; align-items: center; gap: 10px; }header { justify-content: space-between; }h2 { min-width: 0; overflow-wrap: anywhere; }.balance { display: grid; gap: 4px; padding: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-2); }.balance .money { font-size: 24px; font-weight: 650; overflow-wrap: anywhere; }dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 0; }dl > div { padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }dt, p { font-size: 12px; }dt { color: var(--text-muted); }dd { margin: 4px 0 0; font-size: 12px; overflow-wrap: anywhere; }.quality { display: block; margin-top: 6px; }footer { flex-wrap: wrap; }
</style>
