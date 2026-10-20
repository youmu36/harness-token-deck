<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, useId } from 'vue';
import { CircleCheck, CircleHelp, CirclePause, CircleAlert, LoaderCircle } from 'lucide-vue-next';
import { formatDateTime, formatInteger } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import Badge from '../../ui/Badge.vue';
import { collectionIndicator, diagnosticText } from './collectionNotes';
import type { CollectionSnapshot, UsageHarness } from './types';
const props = withDefaults(defineProps<{ status?: CollectionSnapshot; harness: UsageHarness; mode?: TimeZoneMode; error?: string }>(), { mode: 'system' });
const id = useId(); const open = ref(false); const root = ref<HTMLElement>(); const button = ref<HTMLButtonElement>();
const indicator = computed(() => props.error ? { label: '采集需关注', tone: 'warning' as const } : collectionIndicator(props.status, props.harness));
const bridge = inject(BridgeKey);
const detail = bridge ? useQuery('collection.diagnostics', async () => unwrap(await bridge.call('collection.status', { details: true })), { visible: open, events: ['collection.tick', 'sources.changed'] }) : undefined;
const sources = computed(() => (detail?.data.value ?? props.status)?.sources.filter(source => props.harness === 'all' || source.harness === props.harness) ?? []);
const names = { claude: 'Claude Code', codex: 'Codex', zcode: 'Zcode', dsh: 'Deepseek Harness' };
const icon = computed(() => props.status?.paused ? CirclePause : indicator.value.tone === 'success' ? CircleCheck
  : indicator.value.tone === 'danger' || indicator.value.tone === 'warning' ? CircleAlert
  : indicator.value.label === '正在建立检查点' ? LoaderCircle : CircleHelp);
function close() { open.value = false; button.value?.focus({ preventScroll: true }); }
function outside(event: PointerEvent) { if (root.value && !root.value.contains(event.target as Node)) open.value = false; }
onMounted(() => document.addEventListener('pointerdown', outside));
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside));
</script>

<template>
  <div ref="root" class="collection-status" @keydown.esc.stop.prevent="close">
    <button ref="button" type="button" class="status-button" :aria-expanded="open" :aria-controls="`${id}-diagnostics`" @click="open = !open">
      <Badge :tone="indicator.tone"><component :is="icon" :size="14" aria-hidden="true" />{{ indicator.label }}</Badge>
    </button>
    <div v-if="open" :id="`${id}-diagnostics`" class="diagnostics surface" role="region" aria-label="采集诊断">
      <p v-if="error || status?.lastError" class="error" role="alert">{{ error || status?.lastError }}</p>
      <p v-if="!sources.length" class="text-muted">等待连接采集服务</p>
      <section v-for="source in sources" :key="source.harness">
        <h3>{{ names[source.harness] }}</h3>
        <p v-if="source.harness === 'codex'" class="text-muted">每 30 秒检查一次。Codex 长请求的用量通常在请求完成后更新；运行中的数字可能暂时保持不变，完成后会自动补齐。</p>
        <ul v-if="source.diagnostics.length"><li v-for="code in source.diagnostics" :key="code">{{ diagnosticText(code) }}</li></ul>
        <p v-else class="text-muted">暂无采集诊断</p>
        <p>已入账 {{ source.ledgerCount === null ? '—' : formatInteger(source.ledgerCount) }} 条、待计费 {{ source.inboxCount === null ? '—' : formatInteger(source.inboxCount) }} 条</p>
        <dl><dt>采集起点</dt><dd>{{ formatDateTime(source.startedAt, mode) }}</dd><dt>最近检查时间</dt><dd>{{ formatDateTime(source.lastScanAt, mode) }}</dd></dl>
      </section>
      <a href="#/settings/background">去设置</a>
    </div>
  </div>
</template>

<style scoped>
.collection-status { position: relative; }.status-button { border: 0; padding: 0; background: transparent; }
.status-button :deep(.badge) { gap: 5px; }
.diagnostics { position: absolute; top: calc(100% + 8px); right: 0; z-index: 10; width: min(360px, calc(100vw - 80px)); padding: 16px; font-size: 12px; max-height: 420px; overflow-y: auto; }
.diagnostics section { display: grid; gap: 8px; margin-bottom: 16px; }
.diagnostics h3 { font-weight: 600; }.diagnostics ul { padding-left: 18px; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; }dt { color: var(--text-muted); }dd { margin: 0; }.error { color: var(--danger); margin-bottom: 10px; }
</style>
