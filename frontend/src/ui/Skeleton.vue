<template><span class="skeleton" :style="{ width, height }" aria-hidden="true" /></template>
<script setup lang="ts">withDefaults(defineProps<{ width?: string; height?: string }>(), { width: '100%', height: '1em' });</script>
<style scoped>.skeleton { display: inline-block; background: var(--surface-2); border-radius: 4px; }</style>
