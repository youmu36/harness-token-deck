<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch, type DeepReadonly } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useCommand } from '../../shared/command';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import { diagnosticText } from '../usage-common/collectionNotes';
import Button from '../../ui/Button.vue';
import Badge from '../../ui/Badge.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
type Source = DeepReadonly<CommandData<'sources.list'>['sources'][number]>;
const props = withDefaults(defineProps<{ source: Source; revision: number; refresh: () => Promise<void>; mode?: TimeZoneMode }>(), { mode: 'system' });
const bridge = inject(BridgeKey)!;
const path = ref(props.source.path); const error = ref(''); const validationError = ref('');
const validation = ref<CommandData<'sources.validatePath'>>(); const validating = ref(false); const browsing = ref(false);
const savePath = useCommand('sources.setPath'); const detect = useCommand('sources.autoDetect');
const busy = computed(() => savePath.pending.value || detect.pending.value || browsing.value);
let generation = 0; let timer: ReturnType<typeof setTimeout> | undefined;
watch(() => props.source.path, (value, previous) => { if (path.value === previous) path.value = value; });
watch(path, value => {
  const current = ++generation; clearTimeout(timer); validation.value = undefined; validationError.value = ''; validating.value = true;
  timer = setTimeout(async () => {
    try { const result = unwrap(await bridge.call('sources.validatePath', { harness: props.source.harness, path: value })); if (current === generation) validation.value = result; }
    catch (failure) { if (current === generation) validationError.value = asAppError(failure).message; }
    finally { if (current === generation) validating.value = false; }
  }, 250);
}, { immediate: true, flush: 'sync' });
onBeforeUnmount(() => { generation++; clearTimeout(timer); });
async function browse() {
  browsing.value = true; error.value = '';
  try { const result = unwrap(await bridge.call('app.pickPath', { kind: props.source.harness === 'zcode' ? 'file' : 'dir', initial: path.value })); if (result.path !== null) path.value = result.path; }
  catch (failure) { error.value = asAppError(failure).message; } finally { browsing.value = false; }
}
async function save(auto = false) {
  error.value = '';
  try {
    const result = auto ? await detect.run({ expectedRevision: props.revision }) : await savePath.run({ harness: props.source.harness, path: path.value, expectedRevision: props.revision });
    path.value = result.sources.find(s => s.harness === props.source.harness)!.path; await props.refresh();
  } catch (failure) { error.value = asAppError(failure).message; if (asAppError(failure).code === 'SOURCES_REVISION_CONFLICT') { await props.refresh(); error.value += '；路径草稿已保留，请检查后重新确认。'; } }
}
const label = computed(() => props.source.harness === 'codex' ? 'Codex' : props.source.harness === 'zcode' ? 'Zcode' : 'DSH');
const statuses: Record<string, string> = { watching: '监测中', pending: '等待连接', baselining: '正在建立检查点', degraded: '采集需关注', unavailable: '来源不可用' };
</script>
<template><article class="source-card" :aria-label="`${label} 来源`"><header><h3>{{ label }} <Badge :tone="source.status === 'unavailable' ? 'danger' : source.status === 'degraded' ? 'warning' : 'neutral'">{{ statuses[source.status] }}</Badge></h3><Button :disabled="busy" title="采用各个来源的有效默认路径" @click="save(true)">自动定位</Button></header><label :for="`path-${source.harness}`">{{ source.harness === 'zcode' ? '数据库文件' : '来源目录' }}</label><div class="path-controls"><input :id="`path-${source.harness}`" v-model="path" :aria-label="`${label} 来源路径`" :disabled="busy" /><Button :pending="browsing" :disabled="busy" @click="browse">浏览…</Button><Button :pending="savePath.pending.value" :disabled="busy || path === source.path" @click="save()">保存</Button></div><p class="text-muted" role="status">{{ validating ? '正在校验路径…' : validationError || (validation ? validation.available ? `路径存在，类型为${source.harness === 'zcode' ? '文件' : '目录'}` : `路径不可用：${validation.diagnostics.map(diagnosticText).join('；')}。仍可保存，稍后创建来源后会继续检查。` : '') }}</p><dl><div><dt>采集起点</dt><dd>{{ source.startedAt ? formatDateTime(source.startedAt, mode) : '尚未开始' }}</dd></div><div><dt>最近检查</dt><dd>{{ source.lastScanAt ? formatDateTime(source.lastScanAt, mode) : '尚未检查' }}</dd></div><div><dt>已接收 / 已入账 / 待计费</dt><dd>{{ source.identityCount }} / {{ source.ledgerCount }} / {{ source.inboxCount }}</dd></div></dl><ul v-if="source.diagnostics.length"><li v-for="code in source.diagnostics" :key="code">{{ diagnosticText(code) }}</li></ul><InlineAlert v-if="error" severity="danger">{{ error }}</InlineAlert></article></template>
<style scoped>.source-card { display: grid; gap: 12px; padding-top: 16px; }.source-card + .source-card { border-top: 1px solid var(--border); }header, h3 { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }header { justify-content: space-between; }.path-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }.path-controls input { flex: 1 1 240px; min-width: 0; }dl { display: flex; flex-wrap: wrap; gap: 12px 24px; font-size: 12px; }dt { color: var(--text-muted); }dd { margin: 4px 0 0; overflow-wrap: anywhere; }ul { padding-left: 20px; }</style>
