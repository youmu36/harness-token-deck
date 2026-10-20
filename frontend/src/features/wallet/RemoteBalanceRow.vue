<script setup lang="ts">
import { computed, inject } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import { formatRemoteAmount, kindLabels, statusLabels, type RemoteObservation } from './remote';
import Badge from '../../ui/Badge.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ vendorId: string; mode: TimeZoneMode }>();
const emit = defineEmits<{ configure: []; calibrate: [value: RemoteObservation] }>();
const bridge = inject(BridgeKey);
const query = bridge ? useQuery(`remote/${props.vendorId}`, async () => unwrap(await bridge.call('remote.get', { vendorId: props.vendorId })), { deps: [() => props.vendorId], events: [{ name: 'wallet.changed', when: event => !event.vendorIds.length || event.vendorIds.includes(props.vendorId) }] }) : undefined;
const refresh = bridge ? useCommand('remote.refresh') : undefined;
const snapshot = computed(() => query?.data.value);
const observation = computed(() => snapshot.value?.observation);
async function fetchBalance() { try { await refresh?.run({ vendorId: props.vendorId }); await query?.refresh(); } catch { /* Command error is rendered below. */ } }
</script>
<template>
  <section class="remote-balance" aria-label="远程余额">
    <div class="remote-heading"><span>远程额度</span><Badge :tone="snapshot?.status === 'ok' ? 'success' : snapshot?.status === 'unconfigured' || !snapshot ? 'neutral' : 'warning'">{{ statusLabels[snapshot?.status ?? 'unconfigured'] }}</Badge></div>
    <template v-if="observation"><p><span v-if="observation.amountPico !== null" class="money">{{ formatRemoteAmount(observation) }}</span><span v-else-if="observation.kind === 'unlimited'">不限额</span><span v-else>{{ observation.rawQuota }} 配额单位</span> · {{ kindLabels[observation.kind] }}</p>
      <p class="text-muted">{{ observation.source }} · 获取于 {{ formatDateTime(observation.fetchedAt, mode) }}<span v-if="observation.stale"> · 已过期</span></p>
      <p v-if="observation.currencyUncertain" class="text-muted">站点账单币种尚未确认，暂不能用于校准。</p>
      <p v-else-if="observation.originalCurrency === 'CNY'" class="text-muted">原币种 CNY，余额按固定 1:1 约定转换为 USD。</p>
    </template>
    <p v-if="snapshot?.error || query?.error.value || refresh?.error.value" class="error" role="alert">{{ snapshot?.error?.message || query?.error.value?.message || refresh?.error.value?.message }}<span v-if="snapshot?.status === 'auth_failed'">；请在远程账户中更新令牌。</span></p>
    <p v-if="snapshot?.status === 'rate_limited' && snapshot.nextRefreshAt" class="text-muted">可重试时间：{{ formatDateTime(snapshot.nextRefreshAt, mode) }}</p>
    <div class="actions"><Button v-if="snapshot?.account" :pending="refresh?.pending.value || snapshot.refreshing" @click="fetchBalance">刷新远程余额</Button><Button v-if="observation?.canCalibrate" @click="emit('calibrate', observation)">按此余额校准</Button><Button v-if="query?.error.value" @click="query.refresh">重新读取</Button></div>
  </section>
</template>
<style scoped>.remote-balance { display: grid; gap: 8px; padding-top: 12px; border-top: 1px solid var(--border); font-size: 12px; }.remote-heading, .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }.money { color: var(--money); font-variant-numeric: tabular-nums; }p { overflow-wrap: anywhere; }.error { color: var(--danger); }</style>
