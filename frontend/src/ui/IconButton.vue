<template>
  <button class="icon-button" type="button" :aria-label="label" :title="label" :disabled="disabled" @click="$emit('click', $event)"><slot /></button>
</template>
<script setup lang="ts">
defineProps<{ label: string; disabled?: boolean }>();
defineEmits<{ click: [event: MouseEvent] }>();
</script>
<style scoped>.icon-button { width: 32px; height: 32px; display: inline-grid; place-items: center; border: 1px solid transparent; border-radius: var(--radius); background: transparent; }</style>
