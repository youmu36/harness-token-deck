<template><div class="toast-host" aria-live="polite"><div v-for="toast in toasts" :key="toast.id" class="toast" :class="`toast--${toast.kind}`" role="status">{{ toast.message }}<button type="button" aria-label="关闭" @click="dismiss(toast.id)">×</button></div></div></template>
<script setup lang="ts">
import { toasts, dismiss } from './toastState';
</script>
<style scoped>.toast-host { position: fixed; right: 20px; bottom: 20px; z-index: 1000; display: grid; gap: 8px; }.toast { min-width: 220px; padding: 10px 12px; display: flex; justify-content: space-between; gap: 12px; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--info); border-radius: var(--radius); box-shadow: 0 4px 16px rgb(0 0 0 / 15%); }.toast--success { border-left-color: var(--ok); }.toast--danger { border-left-color: var(--danger); }.toast button { border: 0; background: transparent; }</style>
