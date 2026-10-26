<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue';
import type { CommandData } from '../../app/ports';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import { emptyScope, type ExploreScope } from './scope';
import { useModelOptions } from './useModelOptions';
import { priceGroupOptions, priceVersionOptions } from './comparisonOptions';

const props = defineProps<{ modelValue: ExploreScope; draftValue?: ExploreScope; options?: CommandData<'stats.exploreOptions'>; costBasis?: 'local' | 'remote'; simple?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: ExploreScope]; 'update:draftValue': [value: ExploreScope] }>();
const clone = (value: ExploreScope): ExploreScope => JSON.parse(JSON.stringify(value)) as ExploreScope;
const draft = ref(clone(props.draftValue ?? props.modelValue));
const root = ref<HTMLElement | null>(null);
function closeMenus(focus = false) {
  for (const menu of root.value?.querySelectorAll<HTMLDetailsElement>('details[open]') ?? []) {
    menu.open = false; if (focus) menu.querySelector('summary')?.focus();
  }
}
function openMenu(event: Event) {
  const target = event.target as HTMLDetailsElement;
  if (!target.open) return;
  for (const menu of root.value?.querySelectorAll<HTMLDetailsElement>('details[open]') ?? []) if (menu !== target) menu.open = false;
}
const outside = (event: PointerEvent) => { if (!root.value?.contains(event.target as Node)) closeMenus(); };
onMounted(() => document.addEventListener('pointerdown', outside));
onScopeDispose(() => document.removeEventListener('pointerdown', outside));
watch(() => props.modelValue, value => { draft.value = clone(value); }, { deep: true });
watch(draft, value => emit('update:draftValue', clone(value)), { deep: true });
const panel = ref<'vendorIds' | 'groupIds'>(props.simple ? 'groupIds' : 'vendorIds');
const parent = ref<string | null>(null);
const search = ref(''), offset = ref(0), modelOpen = ref(false);
watch(() => props.costBasis, () => { panel.value = 'vendorIds'; parent.value = null; search.value = ''; });
watch([search, panel, parent], () => { offset.value = 0; });
const modelOptions = useModelOptions(modelOpen);
const sources = [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }, { id: 'dsh', name: 'DSH' }, { id: 'zcode', name: 'Zcode' }];
const catalog = ref<Record<string, string>>({});
function remember(key: string, rows: { id: string; name: string }[]) {
  for (const row of rows) catalog.value[`${key}:${row.id}`] = row.name;
}
watch(() => props.options, value => {
  remember('harnesses', sources);
  remember('vendorIds', [{ id: '', name: '未关联供应商' }, ...(value?.vendors ?? [])]);
  remember('groupIds', [{ id: '', name: '信息缺失' }, ...priceGroupOptions(value)]);
  remember('versionIds', [{ id: '', name: '信息缺失' }, ...priceVersionOptions(value)]);
  remember('modelKeys', [{ id: '', name: '未知模型' }, ...(value?.models ?? []), ...(value?.selectedModels ?? [])]);
}, { immediate: true });
watch(modelOptions.rows, rows => remember('modelKeys', rows));
const rows = computed(() => parent.value !== null
  ? [{ id: '', name: '信息缺失' }, ...priceVersionOptions(props.options).filter(row => row.parentId === parent.value)]
  : panel.value === 'vendorIds' ? [{ id: '', name: '未关联供应商' }, ...(props.options?.vendors ?? [])]
    : [{ id: '', name: '信息缺失' }, ...priceGroupOptions(props.options)]);
const filtered = computed(() => rows.value.filter(row => row.name.toLowerCase().includes(search.value.toLowerCase())));
const field = computed(() => parent.value !== null ? 'versionIds' : panel.value);
const selected = (key: keyof ExploreScope, id: string) => draft.value[key]?.includes(id as never) ?? false;
const full = (key: keyof ExploreScope, id: string) => !selected(key, id) && (draft.value[key]?.length ?? 0) >= (key === 'harnesses' ? 4 : 50);
function incompatible(key: keyof ExploreScope, id: string) {
  if (props.costBasis === 'remote' || !props.options?.groupVendors || !Object.keys(props.options.groupVendors).length || !id) return false;
  if (key === 'groupIds' && draft.value.vendorIds?.length) return !props.options.groupVendors[id]?.some(vendor => draft.value.vendorIds?.includes(vendor));
  if (key === 'vendorIds' && draft.value.groupIds?.length) return !draft.value.groupIds.some(group => props.options?.groupVendors[group]?.includes(id));
  return false;
}
function toggle(key: keyof ExploreScope, id: string, checked: boolean) {
  if (checked && (full(key, id) || incompatible(key, id))) return;
  const values = draft.value[key] ?? [];
  draft.value = { ...draft.value, [key]: checked ? [...new Set([...values, id])] : values.filter(value => value !== id) };
  if (key === 'vendorIds' && draft.value.vendorIds?.length) {
    draft.value.groupIds = draft.value.groupIds?.filter(group => !incompatible('groupIds', group));
  }
  if ((key === 'groupIds' || key === 'vendorIds') && draft.value.groupIds?.length) {
    draft.value.versionIds = draft.value.versionIds?.filter(version => version === '' || props.options?.versions.some(row => row.id === version && draft.value.groupIds?.includes(row.parentId ?? '')));
  }
  if (key === 'versionIds' && checked && id && parent.value !== null && draft.value.groupIds?.length && !draft.value.groupIds.includes(parent.value)) {
    if (draft.value.groupIds.length >= 50 || incompatible('groupIds', parent.value)) {
      draft.value.versionIds = draft.value.versionIds?.filter(version => version !== id);
    } else draft.value.groupIds = [...draft.value.groupIds, parent.value];
  }
}
const summary = computed(() => (Object.keys(props.modelValue) as (keyof ExploreScope)[])
  .filter(key => props.costBasis !== 'remote' || key === 'vendorIds')
  .flatMap(key => (props.modelValue[key] ?? []).map(id => catalog.value[`${key}:${id}`] ?? (id || '未知'))).join(' / '));
function apply() { emit('update:modelValue', clone(draft.value)); closeMenus(); }
function reset() { draft.value = emptyScope(); emit('update:modelValue', emptyScope()); }
</script>

<template>
  <section ref="root" class="scope-filters surface" aria-label="组合筛选" @keydown.esc="closeMenus(true)" @toggle.capture="openMenu">
    <div class="scope-row">
      <strong>筛选范围</strong>
      <details v-if="costBasis !== 'remote'"><summary>来源<span>{{ draft.harnesses?.length ? `已选 ${draft.harnesses.length}` : '全部' }}</span></summary>
        <div class="filter-options"><label v-for="choice in sources" :key="choice.id"><input type="checkbox" :checked="selected('harnesses', choice.id)" @change="toggle('harnesses', choice.id, ($event.target as HTMLInputElement).checked)" />{{ choice.name }}</label><Button @click="draft.harnesses = []">全部来源</Button></div>
      </details>
      <details><summary>{{ simple ? '价格组' : costBasis === 'remote' ? '供应商' : '供应商 / 价格组' }}<span>{{ (draft.vendorIds?.length ?? 0) + (costBasis === 'remote' ? 0 : (draft.groupIds?.length ?? 0) + (draft.versionIds?.length ?? 0)) || '全部' }}</span></summary>
        <div class="filter-options provider-options">
          <nav v-if="!simple && costBasis !== 'remote' && parent === null" aria-label="供应商与价格组筛选"><Button :aria-pressed="panel === 'vendorIds'" @click="panel = 'vendorIds'">供应商</Button><Button :aria-pressed="panel === 'groupIds'" @click="panel = 'groupIds'">价格组</Button></nav>
          <div v-if="parent !== null" class="version-heading"><Button @click="parent = null">返回价格组</Button><strong>{{ catalog[`groupIds:${parent}`] }}的倍率版本</strong></div>
          <input v-model="search" aria-label="搜索供应商价格组版本" placeholder="搜索名称" />
          <div class="choice-list"><div v-for="choice in filtered.slice(offset, offset + 50)" :key="choice.id" class="filter-choice">
            <label :title="incompatible(field, choice.id) ? '与当前供应商或价格组没有关联，请先调整已选条件' : undefined"><input type="checkbox" :checked="selected(costBasis === 'remote' ? 'vendorIds' : field, choice.id)" :disabled="full(costBasis === 'remote' ? 'vendorIds' : field, choice.id) || (!selected(field, choice.id) && (incompatible(field, choice.id) || (parent !== null && incompatible('groupIds', parent))))" @change="toggle(costBasis === 'remote' ? 'vendorIds' : field, choice.id, ($event.target as HTMLInputElement).checked)" />{{ choice.name }}</label>
            <Button v-if="costBasis !== 'remote' && panel === 'groupIds' && parent === null && choice.id" :aria-label="`查看 ${choice.name} 的倍率版本`" :disabled="incompatible('groupIds', choice.id)" @click="parent = choice.id; search = ''">倍率版本</Button>
          </div></div>
          <footer><span>{{ filtered.length }} 项 · 同类最多 50 项</span><Button v-if="offset" @click="offset -= 50">上一页</Button><Button v-if="offset + 50 < filtered.length" @click="offset += 50">下一页</Button></footer>
        </div>
      </details>
      <details v-if="!simple && costBasis !== 'remote'" @toggle="modelOpen = ($event.target as HTMLDetailsElement).open"><summary>模型<span>{{ draft.modelKeys?.length ? `已选 ${draft.modelKeys.length}` : '全部' }}</span></summary>
        <div class="filter-options"><input v-model="modelOptions.search.value" aria-label="搜索筛选模型" placeholder="搜索模型" />
          <InlineAlert v-if="modelOptions.query.error.value" severity="danger" retry @retry="modelOptions.query.refresh">{{ modelOptions.query.error.value.message }}</InlineAlert>
          <label v-for="choice in modelOptions.query.data.value ? modelOptions.rows.value : options?.models ?? []" :key="choice.id"><input type="checkbox" :checked="selected('modelKeys', choice.id)" :disabled="full('modelKeys', choice.id)" @change="toggle('modelKeys', choice.id, ($event.target as HTMLInputElement).checked)" />{{ choice.name || '未知模型' }}</label>
          <footer><span>{{ modelOptions.total.value }} 项 · 最多选 50 项</span><Button v-if="modelOptions.offset.value" @click="modelOptions.offset.value -= 100">上一页</Button><Button v-if="modelOptions.next.value !== null" @click="modelOptions.offset.value = modelOptions.next.value!">下一页</Button></footer>
        </div>
      </details>
      <Button variant="primary" @click="apply">应用筛选</Button><Button @click="reset">重置</Button>
    </div>
    <p class="text-muted">已应用：{{ summary || (costBasis === 'remote' ? '全部供应商' : simple ? '全部来源与价格组' : '全部来源、供应商、价格组与模型') }}<span v-if="costBasis !== 'remote'"> · 同类多选合计，条件之间取交集；没有交集时显示零匹配。</span><span v-else> · 来源、模型与价格维度仅用于本地估算，切回后恢复。</span></p>
  </section>
</template>

<style scoped>
.scope-filters{padding:16px 18px;position:relative;z-index:6}.scope-row,nav,footer,.version-heading{display:flex;align-items:center;flex-wrap:wrap;gap:10px}.scope-row>strong{font-size:13px;margin-right:8px}details{position:relative}summary{cursor:pointer;display:flex;gap:14px;align-items:center;min-height:36px;border:1px solid var(--border);border-radius:5px;padding:0 12px;font-size:13px;list-style:none}summary span,footer,.version-heading{color:var(--text-muted);font-size:12px}.filter-options{position:absolute;top:calc(100% + 6px);left:0;width:min(340px,80vw);max-height:420px;overflow:auto;padding:12px;background:var(--surface);border:1px solid var(--border);box-shadow:0 8px 20px #0003;z-index:30;border-radius:6px;display:grid;gap:8px}.provider-options{width:min(460px,80vw)}.filter-options label{display:flex;gap:9px;align-items:center;font-size:13px;padding:6px;min-width:0;overflow-wrap:anywhere}.filter-options input[type=checkbox]{min-height:0;flex-shrink:0}.filter-options input:not([type=checkbox]){width:100%;padding:8px}.filter-choice{display:flex;align-items:center;justify-content:space-between;gap:8px}.filter-choice>button{flex-shrink:0}nav{padding-bottom:8px;border-bottom:1px solid var(--border)}nav [aria-pressed=true]{background:var(--accent-soft);color:var(--accent)}footer{padding-top:8px;border-top:1px solid var(--border);justify-content:flex-end}footer span{margin-right:auto}p{font-size:12px;margin:12px 0 0;line-height:1.7}
@media(max-width:900px){details{position:static}.filter-options{left:18px;right:18px;width:auto;max-height:min(420px,60vh)}}
</style>
