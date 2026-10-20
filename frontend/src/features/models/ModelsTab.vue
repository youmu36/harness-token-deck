<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue';
import { BridgeKey, NavigationGuardKey } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { useToast } from '../../ui/useToast';
import Button from '../../ui/Button.vue';
import Dialog from '../../ui/Dialog.vue';
import ConfirmDialog from '../../ui/ConfirmDialog.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import ExcludedModels from './ExcludedModels.vue';
import ModelScanDialog from './ModelScanDialog.vue';
const bridge = inject(BridgeKey)!;
const pool = useQuery('models.pool', () => bridge.call('models.pool', {}).then(unwrap), { events: ['models.changed', 'pricing.changed'] });
type Draft = { modelId: string; input: string; cache: string; cacheWrite: string; output: string; original: string; missing?: boolean };
const rows = ref<Draft[]>([]); const search = ref(''); const error = ref(''); const draftRevision = ref(0); const conflict = ref(false);
const signature = (row: Pick<Draft, 'input' | 'cache' | 'cacheWrite' | 'output'>) => JSON.stringify([row.input, row.cache, row.cacheWrite, row.output]);
const dirty = computed(() => rows.value.filter(row => signature(row) !== row.original));
watch(pool.data, value => {
  if (!value) return;
  const prior = new Map(rows.value.map(row => [row.modelId, row]));
  rows.value = value.models.map(model => {
    const row = prior.get(model.modelId);
    if (row && signature(row) !== row.original) return { ...row, missing: false };
    const next = { modelId: model.modelId, input: model.rates.input ?? '', cache: model.rates.cache ?? '', cacheWrite: model.rates.cacheWrite ?? '', output: model.rates.output ?? '', original: '' };
    next.original = signature(next); return next;
  });
  const current = new Set(value.models.map(model => model.modelId));
  for (const row of prior.values()) if (!current.has(row.modelId) && signature(row) !== row.original) rows.value.push({ ...row, missing: true });
  if (!dirty.value.length || conflict.value) draftRevision.value = value.revision;
});
const filtered = computed(() => rows.value.filter(row => row.modelId.toLowerCase().includes(search.value.trim().toLowerCase())).sort((a, b) => a.modelId.localeCompare(b.modelId, 'en', { sensitivity: 'base' })));
const saveRates = useCommand('models.saveBaseRates'); const add = useCommand('models.add'); const remove = useCommand('models.remove'); const restore = useCommand('models.restore');
const busy = computed(() => saveRates.pending.value || add.pending.value || remove.pending.value || restore.pending.value);
const toast = useToast(); const adding = ref(false); const name = ref(''); const scanning = ref(false); const removing = ref(''); const leaving = ref(false); let proceed: (() => void) | undefined;
async function refreshed() { await pool.refresh(); }
async function save() {
  error.value = ''; conflict.value = false;
  try { await saveRates.run({ models: dirty.value.map(row => ({ modelId: row.modelId, rates: { input: row.input || null, cache: row.cache || null, cacheWrite: row.cacheWrite || null, output: row.output || null } })), expectedRevision: draftRevision.value }); rows.value = []; await refreshed(); toast.success('模型池价格已保存'); }
  catch (failure) { error.value = asAppError(failure).message; if (asAppError(failure).code === 'PRICING_REVISION_CONFLICT') { conflict.value = true; await refreshed(); } }
}
async function change(action: 'add' | 'remove' | 'restore', modelId: string) {
  error.value = '';
  try {
    const expectedRevision = pool.data.value!.revision;
    if (action === 'remove') await remove.run({ modelId, expectedRevision });
    else await (action === 'add' ? add : restore).run({ modelIds: [modelId], expectedRevision });
    adding.value = false; removing.value = ''; name.value = ''; await refreshed();
  } catch (failure) { error.value = asAppError(failure).message; if (asAppError(failure).code === 'PRICING_REVISION_CONFLICT') await refreshed(); }
}
function leave(action: () => void) { if (busy.value || scanning.value) return; if (dirty.value.length) { proceed = action; leaving.value = true; } else action(); }
const release = inject(NavigationGuardKey, undefined)?.(leave);
const unload = (event: BeforeUnloadEvent) => { if (dirty.value.length) { event.preventDefault(); event.returnValue = ''; } };
window.addEventListener('beforeunload', unload);
onBeforeUnmount(() => { release?.(); window.removeEventListener('beforeunload', unload); });
</script>
<template><section class="models-tab" aria-label="模型池">
  <header><label>搜索模型<input v-model="search" type="search" placeholder="模型名称" /></label><div class="actions"><Button :disabled="busy || !!dirty.length || !pool.data.value || pool.data.value.models.length >= 5000" @click="adding = true">添加模型</Button><Button :disabled="busy || !!dirty.length || !pool.data.value" @click="scanning = true">查找最近使用的模型</Button></div></header>
  <p class="text-muted">模型池价格用于填充价格版本，单位 USD / 百万 token；缓存创建价格留空按 0，其余留空表示未定价。{{ pool.data.value?.models.length ?? 0 }}/5000 个模型。</p>
  <InlineAlert v-if="pool.error.value" severity="danger" retry @retry="refreshed">{{ pool.error.value.message }}</InlineAlert><InlineAlert v-if="error && !adding && !removing" severity="danger">{{ error }}</InlineAlert><InlineAlert v-if="conflict" severity="warning">价格目录已变化，已刷新目录并保留草稿；请检查后重新确认保存。</InlineAlert>
  <div class="table-scroll"><table><thead><tr><th>模型</th><th>输入价格</th><th>输出价格</th><th>缓存命中价格</th><th>缓存创建价格</th><th>操作</th></tr></thead><tbody><tr v-for="row in filtered" :key="row.modelId" :class="{ dirty: signature(row) !== row.original }"><th scope="row">{{ row.modelId }}<span v-if="signature(row) !== row.original" class="text-muted"> · {{ row.missing ? '模型已被移出，草稿保留' : '未保存' }}</span></th><td v-for="field in (['input', 'output', 'cache', 'cacheWrite'] as const)" :key="field"><input v-model="row[field]" :aria-label="`${row.modelId} ${{ input: '输入价格', cache: '缓存命中价格', cacheWrite: '缓存创建价格', output: '输出价格' }[field]}`" inputmode="decimal" :placeholder="field === 'cacheWrite' ? '0' : '未定价'" :disabled="busy" /></td><td><Button :disabled="busy || !!dirty.length" @click="error = ''; removing = row.modelId">移出</Button></td></tr></tbody></table></div>
  <p v-if="pool.loading.value">正在读取模型池…</p><p v-else-if="!filtered.length">{{ search ? '没有匹配的模型' : '模型池为空，请添加模型或从来源扫描' }}</p>
  <footer><span>未保存 {{ dirty.length }} 行<span v-if="dirty.length" class="text-muted"> · 保存后可添加、移出或扫描模型</span></span><Button :disabled="!dirty.length || busy" @click="leave(() => { rows = []; conflict = false; error = ''; refreshed(); })">放弃修改</Button><Button variant="primary" :pending="saveRates.pending.value" :disabled="!dirty.length || dirty.some(row => row.missing)" @click="save">保存全部修改</Button></footer>
  <ExcludedModels :models="pool.data.value?.excluded ?? []" :pending="busy" :disabled="!!dirty.length" @restore="change('restore', $event)" />
  <Dialog v-model:open="adding" title="添加模型" :pending="add.pending.value" :error="error"><form id="add-model" @submit.prevent="change('add', name)"><label>模型名称<input v-model="name" aria-label="模型名称" maxlength="160" required /></label><p class="text-muted">添加被移出的模型会恢复到模型池。</p></form><template #footer><Button :disabled="busy" @click="adding = false; error = ''">取消</Button><Button form="add-model" type="submit" variant="primary" :pending="add.pending.value" :disabled="!name.trim()">添加</Button></template></Dialog>
  <ConfirmDialog :open="!!removing" title="移出模型" :message="`将移出 ${removing} 并删除其基准价；自动同步会跳过此模型，已有价格和账本保留。可在已移出列表恢复。`" :pending="remove.pending.value" :error="error" confirm-label="移出" @update:open="if (!$event) { removing = ''; error = ''; }" @confirm="change('remove', removing)" />
  <ConfirmDialog v-model:open="leaving" title="放弃模型修改" message="未保存的模型池价格修改将被放弃。" confirm-label="放弃修改" @confirm="leaving = false; proceed?.()" />
  <ModelScanDialog v-if="scanning" :revision="pool.data.value!.revision" :refresh="refreshed" @close="scanning = false" @saved="refreshed" />
</section></template>
<style scoped>.models-tab { display: grid; gap: 16px; min-width: 0; padding: 18px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }header, footer, .actions { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }header label { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }.table-scroll { overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }table { width: 100%; border-collapse: collapse; min-width: 740px; }th, td { padding: 10px 8px; border-bottom: 1px solid var(--border); text-align: left; }tbody th { max-width: 220px; overflow-wrap: anywhere; font-weight: 500; }td input { width: 100%; min-width: 84px; max-width: 140px; font-variant-numeric: tabular-nums; }tr.dirty { background: var(--accent-soft); }footer { border-top: 1px solid var(--border); padding-top: 16px; font-size: 12px; }form { display: grid; gap: 16px; }form label { display: grid; gap: 8px; }</style>
