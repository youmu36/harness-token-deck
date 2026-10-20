<script setup lang="ts">
import { computed, inject, onScopeDispose, ref, watch } from 'vue';
import { RefreshCw } from 'lucide-vue-next';
import { BridgeKey, type CommandData } from '../../app/ports';
import { useCommand } from '../../shared/command';
import Button from '../../ui/Button.vue';
const props = defineProps<{ nextPeriodicAt?: string | null; showCountdown?: boolean; paused?: boolean }>();
const emit = defineEmits<{ started: []; completed: [data: CommandData<'collection.scanNow'>] }>();
const bridge = inject(BridgeKey)!;
const deadline = ref<string | null>(props.nextPeriodicAt ?? null);
const now = ref(Date.now());
function accept(value: string | null) {
  deadline.value = value; now.value = Date.now();
}
watch(() => props.nextPeriodicAt, value => accept(value ?? null), { immediate: true });
const unsubscribe = bridge.events.subscribe('collection.tick', event => accept(event.nextPeriodicAt));
let timer: ReturnType<typeof setInterval> | undefined;
const visible = ref(bridge.events.isVisible());
const unsubscribeVisibility = bridge.events.onVisibility(value => { visible.value = value; now.value = Date.now(); });
watch(() => props.showCountdown && visible.value && !props.paused && deadline.value, enabled => {
  if (timer) clearInterval(timer);
  timer = enabled ? setInterval(() => { now.value = Date.now(); }, 1000) : undefined;
}, { immediate: true });
onScopeDispose(() => { if (timer) clearInterval(timer); unsubscribe(); unsubscribeVisibility(); });
const remaining = computed(() => Math.max(0, (deadline.value ? Date.parse(deadline.value) : now.value) - now.value));
const seconds = computed(() => Math.ceil(remaining.value / 1000));
// Notification scans retain the backend deadline; their ticks must not refill the ring.
const sweep = computed(() => `${Math.min(1, remaining.value / 30_000) * 360}deg`);
const command = useCommand('collection.scanNow', { onSuccess: data => { accept(data.nextPeriodicAt); emit('completed', data); } });
async function scan() { if (command.pending.value) return; emit('started'); try { await command.run({}); } catch { /* Show the command error next to the button. */ } }
</script>

<template>
  <div class="scan-control">
    <span v-if="showCountdown && !paused && deadline" class="countdown" :title="`下一次周期扫描：${deadline}`" :aria-label="`距下一次周期扫描 ${seconds} 秒`">
      <span class="countdown-ring" :style="{ '--countdown-sweep': sweep }" aria-hidden="true" />{{ seconds }}<span class="sr-only"> 秒</span>
    </span>
    <Button :disabled="paused" :pending="command.pending.value" @click="scan"><RefreshCw :size="15" :class="{ spinning: command.pending.value }" aria-hidden="true" />{{ command.pending.value ? '正在更新' : '立即更新' }}</Button>
    <span v-if="command.error.value" class="scan-error" role="alert">{{ command.error.value.message }}</span>
  </div>
</template>

<style scoped>
.scan-control { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.scan-control :deep(.button) { display: inline-flex; align-items: center; gap: 6px; }
.scan-control :deep(.button__spinner) { display: none; }
.countdown { position: relative; display: inline-flex; align-items: center; justify-content: center; width: 44px; height: 44px; flex-shrink: 0; font-size: 14px; color: var(--accent); font-variant-numeric: tabular-nums; }
.countdown-ring { position: absolute; inset: 0; border-radius: 50%; background: conic-gradient(var(--accent) 0deg var(--countdown-sweep), var(--border) var(--countdown-sweep) 360deg); mask: radial-gradient(circle, transparent 17px, #000 17.5px); }
.scan-error { color: var(--danger); font-size: 12px; }
.spinning { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }
</style>
