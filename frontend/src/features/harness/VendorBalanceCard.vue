<script setup lang="ts">
import { computed, inject } from 'vue';
import { BridgeKey } from '../../app/ports';
import { AppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { type TimeZoneMode } from '../../shared/time';
import Money from '../../ui/Money.vue';
const props = defineProps<{ groupId?: string | null; vendorId?: string | null; groupLoading?: boolean; groupError?: string; timezone: TimeZoneMode }>();
const bridge = inject(BridgeKey)!;
const query = useQuery('harness.vendorBalance', async () => {
  if (props.groupLoading || props.groupError || !props.vendorId) return undefined;
  const snapshot = unwrap(await bridge.call('wallet.list', {}));
  const vendor = snapshot.vendors.find(vendor => vendor.id === props.vendorId);
  if (!vendor) throw new AppError({ code: 'VENDOR_NOT_FOUND', message: '供应商不存在', retryable: false });
  return vendor;
}, { deps: [() => props.groupId, () => props.vendorId, () => props.groupLoading, () => props.groupError], clearOnChange: true, coalesce: true, events: [{ name: 'wallet.changed', when: event => !!props.vendorId && event.vendorIds.includes(props.vendorId) }] });
const failure = computed(() => props.groupError || query.error.value?.message);
const vendor = computed(() => props.groupLoading || failure.value ? undefined : query.data.value);
const remote = computed(() => vendor.value?.walletVisibility === 'remote' || (vendor.value?.walletVisibility !== 'local' && !!vendor.value?.remoteWallet));
const balance = computed(() => remote.value ? vendor.value?.remoteWallet?.B : vendor.value?.localWallet?.B ?? vendor.value?.B);
const unlinked = computed(() => !props.groupLoading && !props.groupError && !props.vendorId);
</script>

<template>
  <component :is="vendorId && !groupLoading && !groupError ? 'a' : 'div'" class="surface balance-card" :href="vendorId ? `#/wallet/${encodeURIComponent(vendorId)}` : undefined" :title="failure" :aria-busy="groupLoading || query.loading.value || query.refreshing.value">
    <span class="balance-main"><span v-if="!unlinked">{{ vendor?.name ?? '供应商' }}</span><span class="balance-value">余额：<Money v-if="balance != null" :pico="balance" mode="trunc2" /><span v-else class="money">---</span></span></span>
    <span v-if="vendor" class="balance-detail">{{ remote ? '远程计费' : '本地估算' }}</span>
    <span v-if="!unlinked && !vendor" class="balance-detail">{{ failure ? '读取失败' : '正在读取' }}</span>
  </component>
</template>

<style scoped>
.balance-card { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 4px 8px; max-width: 100%; padding: 10px 14px; border-radius: 10px; color: var(--text); text-decoration: none; font-size: 13px; font-variant-numeric: tabular-nums; }
a.balance-card:hover { border-color: var(--accent); }.balance-main { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 8px; overflow-wrap: anywhere; }.balance-value { color: var(--money); font-weight: 600; }.balance-main .money { font-size: inherit; }.balance-detail { font-size: 12px; color: var(--text-muted); }
</style>
