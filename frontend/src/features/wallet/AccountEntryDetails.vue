<script setup lang="ts">
import type { CommandData } from '../../app/ports';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import { formatExactMoney } from '../../shared/money';
defineProps<{ entry: CommandData<'wallet.accountEntries'>['entries'][number]; mode: TimeZoneMode }>();
const kinds: Record<string, string> = {top_up:'充值',manual_top_up:'人工充值',consumption:'消费',interval_revision:'区间重核',adjustment:'校准 / 调整与校准',correction:'类型更正',consumption_correction:'累计消费更正',balance_correction:'余额校准'};
</script>
<template>
  <div class="entry-details">
    <strong>{{ kinds[entry.kind] ?? entry.kind }}</strong>
    <p class="transaction-time" v-if="entry.fromAt && entry.toAt">流水区间：{{ formatDateTime(entry.fromAt, mode) }} → {{ formatDateTime(entry.toAt, mode) }}</p>
    <p class="transaction-time" v-else>流水发生时间：{{ formatDateTime(entry.occurredAt, mode) }}</p>
    <div class="changes"><span>充值变化 <span class="money">{{ formatExactMoney(entry.R, true) }}</span></span><span>消费变化 <span class="money">{{ formatExactMoney(entry.C, true) }}</span></span><span>调整与校准 <span class="money">{{ formatExactMoney(entry.A, true) }}</span></span></div>
    <p>可改判金额：<span class="money">{{ formatExactMoney(entry.availablePico) }}</span></p>
    <p v-if="entry.note" class="text-muted">{{ entry.note }}</p>
  </div>
</template>
<style scoped>
.entry-details{display:grid;gap:8px;text-align:left;font-size:13px;min-width:0}.transaction-time{font-variant-numeric:tabular-nums}.changes{display:flex;flex-wrap:wrap;gap:8px 20px}.money{color:var(--money);font-variant-numeric:tabular-nums}p{overflow-wrap:anywhere}
</style>
