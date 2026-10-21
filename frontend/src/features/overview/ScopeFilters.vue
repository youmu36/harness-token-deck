<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { CommandData } from '../../app/ports';
import Button from '../../ui/Button.vue';
import { emptyScope, type ExploreScope } from './scope';
const props = defineProps<{ modelValue: ExploreScope; options?: CommandData<'stats.exploreOptions'> }>();
const emit = defineEmits<{ 'update:modelValue': [value: ExploreScope] }>();
const draft = ref<ExploreScope>(emptyScope());
watch(() => props.modelValue, value => { draft.value = JSON.parse(JSON.stringify(value)) as ExploreScope; }, { immediate: true, deep: true });
const fields = computed(() => [
  { key: 'harnesses' as const, name: '来源', choices: [{ id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' }, { id: 'dsh', name: 'DSH' }, { id: 'zcode', name: 'Zcode' }] },
  { key: 'vendorIds' as const, name: '供应商', choices: [{ id: '', name: '未关联供应商' }, ...(props.options?.vendors ?? [])] },
  { key: 'groupIds' as const, name: '价格组', choices: [{ id: '', name: '信息缺失' }, ...(props.options?.groups ?? [])] },
  { key: 'versionIds' as const, name: '倍率版本', choices: [{ id: '', name: '信息缺失' }, ...(props.options?.versions ?? []).filter(v => !draft.value.groupIds?.length || draft.value.groupIds.includes(v.parentId ?? ''))] },
  { key: 'modelKeys' as const, name: '模型', choices: props.options?.models ?? [] },
]);
function toggle(key: keyof ExploreScope, id: string, checked: boolean) {
  const values = draft.value[key] ?? [];
  draft.value = { ...draft.value, [key]: checked ? [...values, id] : values.filter(value => value !== id) };
  if (key === 'vendorIds') { draft.value.groupIds = []; draft.value.versionIds = []; }
  if (key === 'groupIds') draft.value.versionIds = [];
}
const summary = computed(() => fields.value.flatMap(field => (props.modelValue[field.key] ?? []).map(id => field.choices.find(c => c.id === id)?.name ?? '历史筛选')).join(' / '));
const search = ref('');
const isSelected = (key: keyof ExploreScope, id: string) => (draft.value[key] as string[] | undefined)?.includes(id) ?? false;
</script>

<template>
  <section class="scope-filters surface" aria-label="组合筛选">
    <div class="scope-row"><strong>组合筛选</strong><details v-for="field in fields" :key="field.key"><summary>{{ field.name }}<span>{{ draft[field.key]?.length ? `已选 ${draft[field.key]!.length}` : '全部' }}</span></summary><div class="filter-options"><input v-if="field.key === 'modelKeys'" v-model="search" aria-label="搜索筛选模型" placeholder="搜索模型" /><label v-for="choice in field.choices.filter(c => field.key !== 'modelKeys' || c.name.toLowerCase().includes(search.toLowerCase())).slice(0, 100)" :key="choice.id"><input type="checkbox" :checked="isSelected(field.key, choice.id)" @change="toggle(field.key, choice.id, ($event.target as HTMLInputElement).checked)" />{{ choice.name }}</label></div></details><Button @click="emit('update:modelValue', JSON.parse(JSON.stringify(draft)))">应用筛选</Button><Button @click="emit('update:modelValue', emptyScope())">重置</Button></div>
    <p class="text-muted">{{ summary || '全部供应商、价格组、倍率版本与模型；来源沿用当前选择' }}<span v-if="summary"> · 不同条件取交集，同类多选合计</span></p>
  </section>
</template>

<style scoped>
.scope-filters{padding:14px 18px;position:relative;z-index:5}.scope-row{display:flex;align-items:center;flex-wrap:wrap;gap:10px}.scope-row>strong{font-size:13px;margin-right:8px}details{position:relative}summary{cursor:pointer;display:flex;gap:12px;align-items:center;min-height:36px;border:1px solid var(--border);border-radius:5px;padding:0 12px;font-size:13px;list-style:none}summary span{color:var(--text-muted);font-size:12px}.filter-options{position:absolute;top:calc(100% + 6px);left:0;width:260px;max-height:330px;overflow:auto;padding:10px;background:var(--surface);border:1px solid var(--border);box-shadow:0 8px 20px #0003;z-index:30;border-radius:6px}.filter-options label{display:flex;gap:9px;align-items:center;font-size:13px;padding:5px}.filter-options input[type=checkbox]{min-height:0}.filter-options input:not([type=checkbox]){width:100%;padding:7px;margin-bottom:8px}p{font-size:12px;margin:10px 0 0;line-height:1.6}
</style>
