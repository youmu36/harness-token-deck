<template><span class="money">{{ formatted }}</span></template>
<script setup lang="ts">import { computed } from 'vue'; import { formatMoney, type MoneyMode } from '../shared/money'; const props = withDefaults(defineProps<{ pico: string | bigint; mode?: MoneyMode }>(), { mode: 'usd4' }); const formatted = computed(() => formatMoney(props.pico, props.mode));</script>
<style scoped>.money { color: var(--money); font-variant-numeric: tabular-nums; }</style>
