<template><div class="empty"><div class="empty__icon" aria-hidden="true">{{ icon }}</div><p>{{ message }}</p><Button v-if="actionLabel" variant="secondary" @click="$emit('action')">{{ actionLabel }}</Button></div></template>
<script setup lang="ts">import Button from './Button.vue'; withDefaults(defineProps<{ message: string; icon?: string; actionLabel?: string }>(), { icon: '∅' }); defineEmits<{ action: [] }>();</script>
<style scoped>.empty { display: grid; justify-items: center; gap: var(--space-3); padding: 40px 16px; color: var(--text-muted); }.empty__icon { font-size: 28px; }</style>
