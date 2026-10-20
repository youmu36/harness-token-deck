<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue';
import { CalendarDays } from 'lucide-vue-next';
import CalendarPopover from './CalendarPopover.vue';
import { convertDateTime, formatDateTimeInput, parseDateTimeInput, type TimeZoneMode } from '../shared/time';

const props = withDefaults(defineProps<{ modelValue?: string; min?: string; max?: string; mode?: TimeZoneMode; id?: string; label?: string; disabled?: boolean; allowFollow?: boolean; following?: boolean }>(), {
  modelValue: '', min: '', max: '', mode: 'system', label: '日期与时间', disabled: false,
});
const emit = defineEmits<{ 'update:modelValue': [value: string]; change: [value: string]; 'validity-change': [valid: boolean]; 'update:following': [value: boolean] }>();
const generatedId = useId();
const fieldId = computed(() => props.id ?? `datetime-${generatedId}`);
const draft = ref(props.modelValue);
const open = ref(false);
const error = ref('');
const root = ref<HTMLElement>();
const calendarButton = ref<HTMLButtonElement>();
const popover = ref<HTMLElement>();
const position = ref({ left: '0px', top: '0px' });
function positionPopover() {
  if (!open.value || !root.value || !popover.value) return;
  const anchor = root.value.querySelector('.datetime-field__input')!.getBoundingClientRect();
  const box = popover.value.getBoundingClientRect();
  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - box.width - 8));
  const below = anchor.bottom + 6;
  const top = below + box.height <= window.innerHeight - 8 ? below : Math.max(8, anchor.top - box.height - 6);
  position.value = { left: `${left}px`, top: `${top}px` };
}
watch(open, async visible => { if (visible) { await nextTick(); positionPopover(); } });
watch(() => props.modelValue, value => { draft.value = value; error.value = ''; });
watch(() => props.mode, (mode, previous) => {
  if (draft.value) {
    try { draft.value = convertDateTime(draft.value, previous, mode); commit(); }
    catch { commit(); }
  }
});
watch(() => props.disabled, disabled => { if (disabled) open.value = false; });
function commit() {
  if (!draft.value) { publish(''); return true; }
  try {
    const date = parseDateTimeInput(draft.value, props.mode);
    if (props.min && date < parseDateTimeInput(props.min, props.mode)) throw new RangeError(`时间不能早于 ${props.min}`);
    if (props.max && date > parseDateTimeInput(props.max, props.mode)) throw new RangeError(`时间不能晚于 ${props.max}`);
    publish(formatDateTimeInput(date, props.mode));
    return true;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '时间无效';
    emit('validity-change', false); return false;
  }
}
function publish(value: string) {
  draft.value = value; error.value = '';
  emit('update:modelValue', value); emit('change', value);
  emit('validity-change', Boolean(value));
}
function choose(value: string) { emit('update:following', false); draft.value = value; commit(); }
function follow() { now(); emit('update:following', !props.following); }
function close(returnFocus = true) { open.value = false; if (returnFocus) calendarButton.value?.focus(); }
function now() {
  emit('update:following', false);
  draft.value = formatDateTimeInput(new Date(), props.mode);
  if (commit()) close();
}
function onOutside(event: PointerEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) close(false);
}
function onEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && open.value) { event.preventDefault(); close(); }
}
onMounted(() => {
  document.addEventListener('pointerdown', onOutside);
  window.addEventListener('resize', positionPopover);
  document.addEventListener('scroll', positionPopover, true);
});
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onOutside);
  window.removeEventListener('resize', positionPopover);
  document.removeEventListener('scroll', positionPopover, true);
});
defineExpose({ commit });
</script>

<template>
  <div ref="root" class="datetime-field" @keydown="onEscape">
    <div class="datetime-field__input">
      <input :id="fieldId" :value="draft" :aria-label="label" :disabled="disabled" type="text" placeholder="YYYY-MM-DD HH:mm"
        :aria-invalid="Boolean(error)" :aria-describedby="error ? `${fieldId}-error` : undefined"
        @input="draft = ($event.target as HTMLInputElement).value; $emit('update:following', false); $emit('validity-change', false)" @blur="commit" @keydown.enter.prevent="commit" />
      <button ref="calendarButton" type="button" aria-label="打开日历" :disabled="disabled" :aria-expanded="open"
        :aria-controls="`${fieldId}-calendar`" aria-haspopup="dialog" @click="open = !open"><CalendarDays :size="17" aria-hidden="true" /></button>
    </div>
    <p v-if="error" :id="`${fieldId}-error`" class="datetime-field__error" role="alert">{{ error }}</p>
    <div v-if="open" ref="popover" :id="`${fieldId}-calendar`" class="datetime-field__popover" :style="position">
      <CalendarPopover :model-value="draft" :min="min" :max="max" :mode="mode" :allow-follow="allowFollow" :following="following"
        @select="choose" @clear="emit('update:following', false); publish('')" @now="now" @follow="follow" @close="close" />
    </div>
    <span v-if="following" class="text-muted">随当前时间变动</span>
  </div>
</template>

<style scoped>
.datetime-field { position: relative; }
.datetime-field__input { display: flex; }
.datetime-field__input input { width: 200px; min-width: 0; padding: 5px 8px; border-radius: var(--radius) 0 0 var(--radius); }
.datetime-field__input button { display: grid; place-items: center; width: 34px; border: 1px solid var(--border); border-left: 0; border-radius: 0 var(--radius) var(--radius) 0; background: var(--surface); }
.datetime-field__error { margin-top: 4px; color: var(--danger); font-size: 12px; }
.datetime-field__popover { position: fixed; z-index: 20; max-height: calc(100vh - 16px); overflow-y: auto; }
</style>
