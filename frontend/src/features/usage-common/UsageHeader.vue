<script setup lang="ts">
import CollectionStatus from './CollectionStatus.vue';
import ScanButton from './ScanButton.vue';
import type { TimeZoneMode } from '../../shared/time';
import type { CollectionSnapshot, UsageHarness } from './types';
defineProps<{ title: string; harness: UsageHarness; status?: CollectionSnapshot; error?: string; mode: TimeZoneMode; countdown: boolean }>();
defineEmits<{ scanning: []; scanned: [] }>();
</script>
<template>
  <header class="usage-header">
    <div class="usage-title"><p class="breadcrumb">用量账本 / {{ harness === 'all' ? '数据统计与分析' : title.replace(' 统计', '') }}</p><h1>{{ title }}</h1></div>
    <div class="header-right"><div class="usage-actions"><ScanButton :show-countdown="countdown" :next-periodic-at="status?.nextPeriodicAt" :paused="status?.paused" @started="$emit('scanning')" @completed="$emit('scanned')" /><CollectionStatus :harness="harness" :status="status" :error="error" :mode="mode" /></div><div v-if="$slots.balance" class="header-balance"><slot name="balance" /></div></div>
  </header>
</template>
<style scoped>
.usage-header { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 18px; padding-bottom: 18px; border-bottom: 1px solid var(--border); }
.usage-title h1 { font-size: 28px; }
.breadcrumb { color: var(--text-muted); font-size: 12px; margin-bottom: 7px; }
.header-right { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; min-width: 0; }
.usage-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 16px; align-self: start; }
.usage-actions :deep(.button) { min-height: 40px; padding-inline: 16px; }
.header-balance { max-width: 100%; }
.collection-meta { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 10px 24px; margin-top: 12px; font-size: 12px; color: var(--text-muted); }
.meta-warning { color: var(--warn); }
@media (max-width: 700px) { .usage-header { grid-template-columns: minmax(0, 1fr); }.usage-actions { justify-content: start; }.header-right { align-items: flex-start; } }
</style>
