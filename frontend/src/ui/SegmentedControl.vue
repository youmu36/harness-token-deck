<script setup lang="ts" generic="T extends string | number">
import type { SegmentedOption } from './segmented';
const props = withDefaults(defineProps<{ modelValue: T; options: readonly SegmentedOption<T>[]; label: string; orientation?: 'horizontal' | 'vertical' }>(), { orientation: 'horizontal' });
const emit = defineEmits<{ 'update:modelValue': [value: T] }>();
function select(option: SegmentedOption<T>) { if (!option.disabled) emit('update:modelValue', option.value); }
function onKeydown(event: KeyboardEvent) {
  const delta = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
  if (delta === undefined && event.key !== 'Home' && event.key !== 'End') return;
  const buttons = Array.from((event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('button:not([disabled])'));
  if (!buttons.length) return;
  event.preventDefault();
  const current = buttons.indexOf(event.target as HTMLButtonElement);
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (current + (delta ?? 0) + buttons.length) % buttons.length;
  const button = buttons[index]!;
  button.focus();
  const option = props.options.find(item => String(item.value) === button.dataset.value);
  if (option) select(option);
}
</script>

<template>
  <div class="segmented" :class="orientation" role="radiogroup" :aria-label="label" :aria-orientation="orientation" @keydown="onKeydown">
    <button v-for="option in options" :key="option.value" :data-value="option.value" class="segmented__item"
      :class="{ selected: option.value === modelValue }" :disabled="option.disabled" type="button" role="radio"
      :aria-checked="option.value === modelValue" :tabindex="option.value === modelValue || (!options.some(o => o.value === modelValue && !o.disabled) && options.find(o => !o.disabled) === option) ? 0 : -1"
      @click="select(option)">{{ option.label }}</button>
  </div>
</template>

<style scoped>
.segmented { display: inline-flex; padding: 3px; gap: 3px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-2); }
.segmented.vertical { flex-direction: column; }
.segmented__item { min-height: 30px; border: 0; border-radius: 5px; padding: 4px 10px; background: transparent; }
.segmented__item.selected { color: var(--accent-contrast); background: var(--accent); }
.segmented__item:disabled { opacity: 1; color: var(--text-muted); }
</style>
