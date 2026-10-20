<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue';
const props = defineProps<{ modelValue: number; maximum: number; label: string }>();
const emit = defineEmits<{ 'update:modelValue': [value: number]; change: [] }>();
const open = ref(false); const root = ref<HTMLElement>(); const list = ref<HTMLElement>();
const count = computed(() => props.maximum + 1);
const values = computed(() => Array.from({ length: count.value * 3 }, (_, index) => ({ index, value: index % count.value })));
const rowHeight = () => list.value?.querySelector<HTMLElement>('button')?.offsetHeight || 34;
async function toggle() { open.value = !open.value; if (open.value) { await nextTick(); focus(count.value + props.modelValue); } }
function focus(index: number) {
  list.value?.querySelector<HTMLElement>(`[data-index="${index}"]`)?.focus({ preventScroll: true });
  if (list.value) list.value.scrollTop = index * rowHeight() - (list.value.clientHeight || 180) / 2 + rowHeight() / 2;
}
function loop() {
  if (!list.value) return;
  const cycle = count.value * rowHeight();
  if (list.value.scrollTop < cycle / 2) list.value.scrollTop += cycle;
  else if (list.value.scrollTop > cycle * 2) list.value.scrollTop -= cycle;
}
function close() { open.value = false; root.value?.querySelector('button')?.focus(); }
function choose(value: number) { emit('update:modelValue', value); emit('change'); close(); }
function outside(event: PointerEvent) { if (!root.value?.contains(event.target as Node)) open.value = false; }
function key(event: KeyboardEvent, value: number) {
  const delta = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
  if (delta || event.key === 'Home' || event.key === 'End') {
    event.preventDefault();
    focus(count.value + (event.key === 'Home' ? 0 : event.key === 'End' ? props.maximum : (value + delta + count.value) % count.value));
  }
}
document.addEventListener('pointerdown', outside);
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside));
</script>
<template><div ref="root" class="time-list" @keydown.esc.stop.prevent="close"><button type="button" :aria-label="label" aria-haspopup="listbox" :aria-expanded="open" @click="toggle">{{ String(modelValue).padStart(2, '0') }}</button><div v-if="open" ref="list" class="options" role="listbox" :aria-label="label" @scroll="loop"><button v-for="item in values" :key="item.index" type="button" role="option" :data-index="item.index" :data-value="item.value" :aria-selected="item.index === count + modelValue" @click="choose(item.value)" @keydown="key($event, item.value)">{{ String(item.value).padStart(2, '0') }}</button></div></div></template>
<style scoped>.time-list { position: relative; }.time-list > button { min-width: 64px; padding: 6px; border-radius: 4px; }.options { position: absolute; bottom: 100%; left: 0; width: 72px; height: 180px; overflow-y: auto; overscroll-behavior: contain; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; z-index: 2; }.options button { display: block; width: 100%; height: 34px; border: 0; padding: 7px; background: transparent; }.options button[aria-selected=true] { background: var(--accent-soft); color: var(--accent); }</style>
