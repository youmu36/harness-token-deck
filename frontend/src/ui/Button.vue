<template>
  <button class="button" :class="`button--${variant}`" :disabled="disabled || pending" :aria-busy="pending" :type="type" @click="$emit('click', $event)">
    <span v-if="pending" class="button__spinner" aria-hidden="true" />
    <slot />
  </button>
</template>
<script setup lang="ts">
withDefaults(defineProps<{ variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; pending?: boolean; disabled?: boolean; type?: 'button' | 'submit' | 'reset' }>(), { variant: 'secondary', pending: false, disabled: false, type: 'button' });
defineEmits<{ click: [event: MouseEvent] }>();
</script>
<style scoped>
.button { min-height: 32px; padding: 0 var(--space-3); border: 1px solid var(--border); border-radius: var(--radius); transition: background var(--motion-duration), border-color var(--motion-duration); }
.button--primary { color: var(--accent-contrast); background: var(--accent); border-color: var(--accent); }
.button--secondary { color: var(--text); background: var(--button-bg); border-color: var(--button-border); }
.button--secondary:hover:not(:disabled) { background: var(--accent-soft); border-color: var(--accent); }
.button--ghost { background: transparent; border-color: transparent; }
.button--danger { color: var(--accent-contrast); background: var(--danger); border-color: var(--danger); }
.button:disabled { opacity: 1; color: var(--text-muted); background: var(--surface-2); border-color: var(--border); }
.button__spinner { display: inline-block; width: 13px; height: 13px; margin-right: 6px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; vertical-align: -2px; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
