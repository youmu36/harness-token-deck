<script setup lang="ts">
import Skeleton from './Skeleton.vue';
withDefaults(defineProps<{ title: string; value?: string; fullValue?: string; unit?: string; description?: string; loading?: boolean; money?: boolean }>(), { value: '--', loading: false, money: false });
</script>

<template>
  <article class="metric surface" :aria-busy="loading">
    <div class="metric__header"><h3>{{ title }}</h3><slot name="badge" /></div>
    <div v-if="loading" class="metric__loading"><Skeleton height="34px" width="70%" /><span class="sr-only">加载中</span></div>
    <template v-else>
      <p class="metric__value" :class="{ money }" :title="fullValue"><slot name="value">{{ value }}</slot> <span v-if="unit" class="metric__unit">{{ unit }}</span></p>
      <p v-if="description" class="text-muted metric__description">{{ description }}</p>
    </template>
  </article>
</template>

<style scoped>
.metric { padding: 18px; min-height: 132px; min-width: 0; border-radius: 12px; border-color: var(--metric-border); box-shadow: 0 0 20px var(--metric-glow), inset 0 1px 0 var(--metric-glow); }
.metric__header { display: flex; justify-content: space-between; gap: var(--space-2); }
.metric__header h3 { font-size: 13px; font-weight: 500; color: var(--text-muted); }
.metric__value { margin-top: 9px; font-size: 26px; font-weight: 600; line-height: 1.3; overflow-wrap: anywhere; }
.metric__unit { font-size: 14px; font-weight: 400; }
.metric__description { margin-top: 2px; font-size: 12px; overflow-wrap: anywhere; }
.metric__loading { margin-top: var(--space-3); }
@media (max-width: 1200px) { .metric { padding: 15px 12px; } }
</style>
