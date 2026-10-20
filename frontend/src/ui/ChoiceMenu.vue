<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, useId, watch } from 'vue';
import { Check, ChevronDown } from 'lucide-vue-next';
const props = defineProps<{ modelValue: string; label: string; placeholder?: string; options: readonly { value: string; label: string }[] }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const id = useId();
const open = ref(false);
const root = ref<HTMLElement>();
const trigger = ref<HTMLButtonElement>();
const selected = computed(() => props.options.find(option => option.value === props.modelValue)?.label ?? props.placeholder ?? '请选择');
function close(focus = false) { open.value = false; if (focus) trigger.value?.focus(); }
async function show() { open.value = true; await nextTick(); }
async function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
  if (event.key === 'Tab') { close(); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  if (!open.value) { await show(); ((root.value?.querySelector('[aria-checked="true"]') ?? root.value?.querySelector('[role="radio"]')) as HTMLElement | null)?.focus(); return; }
  const items = [...root.value!.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
  const index = items.indexOf(document.activeElement as HTMLButtonElement);
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length;
  items[next]?.focus();
}
function outside(event: PointerEvent) { if (!root.value?.contains(event.target as Node)) close(); }
function focusout(event: FocusEvent) { if (!root.value?.contains(event.relatedTarget as Node)) close(); }
watch(open, value => { if (value) document.addEventListener('pointerdown', outside); else document.removeEventListener('pointerdown', outside); });
onScopeDispose(() => document.removeEventListener('pointerdown', outside));
function select(value: string) { emit('update:modelValue', value); close(true); }
</script>

<template>
  <div ref="root" class="choice-menu" @keydown="keydown" @focusout="focusout">
    <button ref="trigger" type="button" class="choice-trigger" :aria-label="`${label}：${selected}`" :aria-expanded="open" :aria-controls="id" @click="open ? close() : show()">
      <span class="choice-label">{{ label }}</span><span>{{ selected }}</span><ChevronDown :size="15" aria-hidden="true" />
    </button>
    <div v-if="open" :id="id" class="choice-options" role="radiogroup" :aria-label="label">
      <button v-for="option in options" :key="option.value" type="button" role="radio" :aria-checked="option.value === modelValue" :data-value="option.value" @click="select(option.value)">
        <span>{{ option.label }}</span><Check v-if="option.value === modelValue" :size="15" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.choice-menu { position: relative; }
.choice-trigger { display: flex; align-items: center; gap: 10px; min-height: 36px; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.choice-label { color: var(--text-muted); font-size: 12px; }
.choice-trigger[aria-expanded='true'] { border-color: var(--accent); }
.choice-options { position: absolute; inset: calc(100% + 6px) auto auto 0; z-index: 20; min-width: max(100%, 180px); padding: 5px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: 0 8px 20px rgb(0 0 0 / 18%); }
.choice-options button { display: flex; align-items: center; justify-content: space-between; gap: 16px; width: 100%; min-height: 36px; padding: 6px 10px; border: 0; border-radius: 4px; background: transparent; text-align: left; }
.choice-options button:hover, .choice-options button[aria-checked='true'] { background: var(--accent-soft); color: var(--accent); }
</style>
