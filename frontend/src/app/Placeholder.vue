<script setup lang="ts">
import { ChartColumn, Wallet, Tag, Settings } from 'lucide-vue-next';
import EmptyState from '../ui/EmptyState.vue';
defineProps<{ title: string; description: string; kind?: 'usage' | 'wallet' | 'pricing' | 'settings' }>();
const icons = { usage: ChartColumn, wallet: Wallet, pricing: Tag, settings: Settings };
</script>
<template>
  <header class="page-heading"><h1>{{ title }}</h1><p class="text-muted">{{ description }}</p></header>
  <section class="surface placeholder" :aria-label="title">
    <component :is="icons[kind ?? 'usage']" :size="32" class="placeholder-icon" aria-hidden="true" />
    <EmptyState message="页面内容正在准备中" />
  </section>
</template>
<style scoped>
.page-heading { margin-bottom: 24px; }
.page-heading p { margin-top: 8px; }
.placeholder { min-height: 350px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; }
.placeholder-icon { color: var(--text-faint); }
</style>
