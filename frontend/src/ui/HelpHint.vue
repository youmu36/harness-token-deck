<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId, watch } from 'vue';
import { CircleHelp } from 'lucide-vue-next';
defineProps<{ label: string; text: string }>();
const id = `help-${useId()}`;
const open = ref(false);
const trigger = ref<HTMLElement>(); const panel = ref<HTMLElement>();
const position = ref({ top: '0px', left: '0px' });
async function place() {
  await nextTick();
  if (!open.value || !trigger.value || !panel.value) return;
  const rect = trigger.value.getBoundingClientRect();
  const box = panel.value.getBoundingClientRect();
  const top = rect.bottom + 8 + box.height <= innerHeight - 12 ? rect.bottom + 8 : Math.max(12, rect.top - box.height - 8);
  position.value = { top: `${top}px`, left: `${Math.max(12, Math.min(rect.left, innerWidth - box.width - 12))}px` };
}
const close = () => { open.value = false; };
watch(open, value => {
  if (value) { void place(); window.addEventListener('scroll', close, true); window.addEventListener('resize', close); }
  else { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close); }
});
onBeforeUnmount(() => { window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close); });
</script>
<template>
  <span class="help-hint" @mouseenter="open = true" @mouseleave="open = false" @focusin="open = true" @focusout="open = false" @keydown.esc="open = false">
    <button ref="trigger" type="button" :aria-label="label" :aria-describedby="open ? id : undefined" @click="open = true"><CircleHelp :size="15" aria-hidden="true" /></button>
    <Teleport to="body"><div v-if="open" :id="id" ref="panel" :style="position" class="help-hint__panel" role="tooltip">{{ text }}</div></Teleport>
  </span>
</template>
<style scoped>
.help-hint { display: inline-flex; vertical-align: middle; margin-left: 5px; }.help-hint button { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border: 0; border-radius: 4px; background: transparent; color: var(--text-muted); }.help-hint button:hover, .help-hint button:focus-visible { color: var(--accent); background: var(--accent-soft); }
.help-hint__panel { position: fixed; z-index: 1100; width: min(440px, calc(100vw - 24px)); max-height: calc(100vh - 24px); overflow-y: auto; padding: 16px 18px; border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 8px 28px rgb(0 0 0 / 18%); color: var(--text); background: var(--surface); font-size: 13px; line-height: 1.8; white-space: pre-line; overflow-wrap: anywhere; pointer-events: none; }
</style>
