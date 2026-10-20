<script setup lang="ts">
defineProps<{ label: string; modelValue: string; items: readonly { value: string; label: string; count?: number }[] }>();
defineEmits<{ 'update:modelValue': [value: string] }>();
</script>

<template>
  <nav class="section-nav" :aria-label="label">
    <button v-for="item in items" :key="item.value" type="button" :aria-current="modelValue === item.value ? 'page' : undefined"
      @click="$emit('update:modelValue', item.value)">{{ item.label }}<span v-if="item.count !== undefined">{{ item.count }}</span></button>
  </nav>
</template>

<style scoped>
.section-nav { display: flex; flex-wrap: wrap; gap: 4px; border-bottom: 1px solid var(--border); }
button { display: inline-flex; align-items: center; gap: 8px; padding: 12px 16px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-muted); }
button:hover { background: var(--surface-2); color: var(--text); }
button[aria-current='page'] { border-bottom-color: var(--accent); color: var(--accent); font-weight: 600; }
button span { padding: 0 6px; border-radius: 4px; font-size: 11px; background: var(--surface-2); color: var(--text-muted); }
</style>
