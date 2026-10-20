<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { X } from 'lucide-vue-next';
import InlineAlert from './InlineAlert.vue';
const props = withDefaults(defineProps<{ open: boolean; title: string; pending?: boolean; error?: string }>(), { pending: false });
const emit = defineEmits<{ 'update:open': [value: boolean]; close: [] }>();
const dialog = ref<HTMLDialogElement>();
const titleId = `dialog-title-${useId()}`;
let opener: HTMLElement | null = null;
let generation = 0;
let disposed = false;
const focusable = 'button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])';
function canFocus(target: HTMLElement) {
  if (target.tabIndex < 0 || target.getAttribute('type') === 'hidden' || target.closest('[hidden], [inert]')) return false;
  for (let element: HTMLElement | null = target; element && element !== dialog.value; element = element.parentElement) {
    const style = getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
  }
  return true;
}
watch(() => props.open, async open => {
  const current = ++generation;
  if (open) opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  await nextTick();
  if (disposed || current !== generation || !dialog.value) return;
  if (open) {
    if (!dialog.value.open) dialog.value.showModal();
    const targets = [...dialog.value.querySelectorAll<HTMLElement>(focusable)].filter(canFocus);
    const target = targets.find(item => item.closest('.dialog__body'))
      ?? targets.find(item => item.closest('.dialog__footer')) ?? targets[0];
    (target ?? dialog.value).focus({ preventScroll: true });
  } else if (dialog.value.open) dialog.value.close();
}, { immediate: true });
function onCancel(event: Event) {
  event.preventDefault();
  if (!props.pending) emit('update:open', false);
}
function onNativeClose() {
  if (!disposed) {
    if (props.open) emit('update:open', false);
    emit('close');
  }
  restoreFocus();
}
function restoreFocus() {
  const target = opener; opener = null;
  if (target?.isConnected) target.focus({ preventScroll: true });
}
function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Tab' || !dialog.value) return;
  const targets = [...dialog.value.querySelectorAll<HTMLElement>(focusable)].filter(canFocus);
  const first = targets[0]; const last = targets.at(-1);
  if (!first || !last) { event.preventDefault(); dialog.value.focus(); return; }
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
onBeforeUnmount(() => {
  disposed = true; generation++;
  if (dialog.value?.open) { dialog.value.close(); restoreFocus(); }
});
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="dialog" :aria-labelledby="titleId" :aria-busy="pending" tabindex="-1" @cancel="onCancel" @close="onNativeClose" @keydown="onKeydown">
      <div class="dialog__header"><h2 :id="titleId">{{ title }}</h2><button type="button" aria-label="关闭" :disabled="pending" @click="$emit('update:open', false)"><X :size="18" aria-hidden="true" /></button></div>
      <div class="dialog__body"><slot /><InlineAlert v-if="error" severity="danger" class="dialog__error">{{ error }}</InlineAlert></div>
      <div v-if="$slots.footer" class="dialog__footer"><slot name="footer" /></div>
    </dialog>
  </Teleport>
</template>

<style scoped>
.dialog { width: min(560px, calc(100vw - 32px)); max-height: calc(100vh - 48px); padding: 0; color: var(--text); background: var(--overlay-bg); border: 1px solid var(--border); border-radius: var(--radius); }
.dialog[open] { display: flex; flex-direction: column; }
.dialog::backdrop { background: rgb(0 0 0 / 40%); }
.dialog__header, .dialog__footer { display: flex; flex-shrink: 0; align-items: center; justify-content: space-between; padding: var(--space-4); border-bottom: 1px solid var(--border); }
.dialog__header button { display: grid; place-items: center; width: 32px; height: 32px; border: 0; border-radius: 4px; background: transparent; }
.dialog__body { min-height: 0; overflow-y: auto; padding: var(--space-4); }
.dialog__error { margin-top: var(--space-3); }
.dialog__footer { justify-content: flex-end; gap: var(--space-2); border-top: 1px solid var(--border); border-bottom: 0; }
</style>
