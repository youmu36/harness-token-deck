<script setup lang="ts">
import type { Catalog } from './types';
import Badge from '../../ui/Badge.vue';
import { harnessNames } from '../harness/types';
defineProps<{ catalog: Catalog; modelValue?: string }>();
defineEmits<{ 'update:modelValue': [id: string] }>();
</script>
<template><aside class="group-list" aria-label="价格组列表"><p class="text-muted">价格组 {{ catalog.groups.length }}/100</p><button v-for="group in catalog.groups" :key="group.id" type="button" :aria-pressed="group.id === modelValue" @click="$emit('update:modelValue', group.id)"><strong>{{ group.name }}</strong><Badge :tone="group.disabled ? 'neutral' : group.harnesses.length ? 'success' : 'neutral'">{{ group.disabled ? '已停用' : group.harnesses.length ? `使用中 ${group.harnesses.map(h => harnessNames[h]).join('、')}` : '未使用' }}</Badge></button></aside></template>
<style scoped>.group-list { display: grid; align-content: start; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }.group-list > p { font-size: 12px; margin-bottom: 4px; }button { display: grid; gap: 8px; padding: 12px; text-align: left; border: 1px solid transparent; border-radius: 6px; background: transparent; min-width: 0; }button strong { overflow-wrap: anywhere; font-size: 13px; }button[aria-pressed=true] { background: var(--accent-soft); border-color: var(--accent); }button:hover { background: var(--surface); }button .badge { justify-self: start; white-space: normal; }@media(max-width:700px) { .group-list { grid-template-columns: repeat(2, minmax(0, 1fr)); padding-bottom: 12px; }.group-list p { grid-column: 1 / -1; } }</style>
