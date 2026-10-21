<script setup lang="ts">
import { ref, watch } from 'vue';
import { Plus } from 'lucide-vue-next';
import { usePricingCatalog } from './usePricingCatalog';
import GroupVendorLink from './GroupVendorLink.vue';
import GroupList from './GroupList.vue';
import GroupDetail from './GroupDetail.vue';
import VersionList from './VersionList.vue';
import PriceEditor from './PriceEditor.vue';
import PricingDialog from './PricingDialog.vue';
import type { Version } from './types';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import ModelsTab from '../models/ModelsTab.vue';
import UnpricedTab from './UnpricedTab.vue';
import SectionNav from '../../ui/SectionNav.vue';
import ManagementLayout from '../../ui/ManagementLayout.vue';
import Badge from '../../ui/Badge.vue';
import { versionName } from '../harness/types';
import type { GuideTarget } from '../../app/router';
import { vGuideFocus } from '../../shared/guideFocus';
const props = defineProps<{ models?: boolean; unpriced?: boolean; versions?: boolean; guideTarget?: GuideTarget }>();
const { catalog, pool, wallet, preferences, groupId, versionId, group, version, selectGroup, saved } = usePricingCatalog();
const editor = ref<InstanceType<typeof PriceEditor>>(); const section = ref('versions');
watch(() => props.guideTarget, target => { if (target) leave(() => { section.value = target === 'rates' ? 'rates' : 'versions'; }); }, { immediate: true });
watch(() => props.versions, value => { if (value) leave(() => { section.value = 'versions'; }); });
function changeGroup(id: string) { if (id !== groupId.value) leave(() => { selectGroup(id); section.value = 'rates'; }); }
function changeVersion(id: string) { if (id !== versionId.value) leave(() => { versionId.value = id; }); }
function changeSection(value: string) { if (value !== section.value) leave(() => { section.value = value; }); }
function pickVersion(event: Event) { const select = event.target as HTMLSelectElement; const id = select.value; select.value = versionId.value; changeVersion(id); }
function leave(action: () => void) { if (editor.value) editor.value.requestLeave(action); else action(); }
function editUnpriced(g: string, v: string) { selectGroup(g); versionId.value = v; section.value = 'rates'; window.location.hash = '#/pricing'; }
const action = ref<'create' | 'rename' | 'copy' | 'disable' | 'delete' | 'addVersion' | 'label' | 'activate' | 'disableVersion' | 'deleteVersion'>();
const dialogVersion = ref<Version>();
function open(value: NonNullable<typeof action.value>, v?: Version) { leave(() => { dialogVersion.value = v; action.value = value; }); }
</script>
<template>
  <div class="pricing-page"><header class="page-header"><div><p class="breadcrumb">管理 / 定价</p><h1>{{ props.unpriced ? '未定价模型相关' : props.models ? '模型池' : '价格与模型' }}</h1></div><Button v-if="!models && !unpriced" v-guide-focus="guideTarget === 'group' || (!!guideTarget && !catalog.data.value?.groups.length)" variant="primary" :disabled="!catalog.data.value || catalog.data.value.groups.length >= 100" @click="open('create')"><Plus :size="16" />新建价格组</Button></header>
    <p v-if="!models && !unpriced" class="setup-hint text-muted">先前往模型池添加模型，再编辑价格组</p>
    <nav class="tabs" aria-label="价格与模型标签"><a href="#/pricing" :aria-current="!models && !unpriced ? 'page' : undefined">价格组</a><a href="#/models" :aria-current="models ? 'page' : undefined">模型池</a><a href="#/unpriced" :aria-current="unpriced ? 'page' : undefined">未定价模型相关</a></nav>
    <InlineAlert v-if="catalog.error.value || pool.error.value" severity="danger" retry @retry="catalog.refresh(); pool.refresh()">{{ catalog.error.value?.message || pool.error.value?.message }}</InlineAlert>
    <UnpricedTab v-if="unpriced" @edit="editUnpriced" />
    <ModelsTab v-else-if="models" :guide-focus="guideTarget === 'models'" />
    <ManagementLayout v-else-if="catalog.data.value">
      <template #rail><GroupList :catalog="catalog.data.value" :model-value="groupId" @update:model-value="changeGroup" /></template>
      <template v-if="group">
        <div class="group-heading"><h2>{{ group.name }}</h2><Badge :tone="group.disabled ? 'neutral' : 'success'">{{ group.disabled ? '已停用 · 只读' : '可用价格组' }}</Badge></div>
        <p v-if="group.historical" class="text-muted">历史价格组仅用于统计分析，不关联供应商，也不计入资金流水。</p><GroupVendorLink v-else :group-id="group.id" :vendor-id="group.vendorId" :wallet="wallet.data.value" :leave="leave" @saved="catalog.refresh(); wallet.refresh()" />
        <SectionNav :model-value="section" label="价格组二级导航" :items="[{ value: 'versions', label: '倍率版本管理', count: group.versions.length }, { value: 'rates', label: '价格编辑' }, { value: 'settings', label: '价格组设置' }]" @update:model-value="changeSection" />
        <template v-if="section === 'rates'">
          <div class="version-picker"><label>编辑版本<select v-guide-focus="guideTarget === 'rates'" aria-label="编辑价格版本" :value="versionId" @change="pickVersion"><option v-for="item in group.versions" :key="item.id" :value="item.id">{{ versionName(item) }}{{ item.current ? ' · 当前生效' : '' }}</option></select></label><span class="text-muted">USD / 百万 token · 创建价留空按 0，其余留空表示未定价</span></div>
          <PriceEditor v-if="version" ref="editor" :version="version" :pool="pool.data.value" :readonly="group.disabled || !!version.disabled" :revision="catalog.data.value.revision" @saved="catalog.refresh()" @conflict="catalog.refresh()" />
        </template>
        <VersionList v-else-if="section === 'versions'" :guide-focus="guideTarget === 'versions'" :group="group" :model-value="versionId" :mode="preferences.data.value?.preferences.timezone ?? 'system'" @update:model-value="changeVersion($event); section = 'rates'" @add="open('addVersion')" @rename="open('label', $event)" @activate="open('activate', $event)" @disable="open('disableVersion', $event)" @delete="open('deleteVersion', $event)" />
        <GroupDetail v-else :group="group" :vendor-name="wallet.data.value?.vendors.find(v => v.id === group?.vendorId)?.name" @action="open($event)" />
      </template>
      <p v-else>暂无价格组，点击“新建价格组”开始。</p>
    </ManagementLayout>
    <p v-else-if="catalog.loading.value">正在读取价格目录…</p>
    <PricingDialog v-if="action" :key="action" :action="action" :group="group" :version="dialogVersion" :revision="catalog.data.value?.revision ?? 0" :refresh="catalog.refresh" @close="action = undefined" @saved="saved" />
  </div>
</template>
<style scoped>
.pricing-page { display: grid; gap: 24px; }.pricing-page > * { min-width: 0; }.page-header, .group-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }.page-header button { display: inline-flex; align-items: center; gap: 6px; }.breadcrumb { font-size: 12px; color: var(--text-muted); margin-bottom: 12px; }
.setup-hint { font-size: 12px; margin-top: -12px; }
.tabs { display: flex; gap: 24px; border-bottom: 1px solid var(--border); }.tabs a { padding: 12px 4px; color: var(--text-muted); text-decoration: none; }.tabs a[aria-current=page] { color: var(--accent); border-bottom: 2px solid var(--accent); font-weight: 600; }
.version-picker { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; justify-content: space-between; font-size: 12px; }.version-picker label { display: flex; align-items: center; gap: 12px; }.version-picker select { min-width: 160px; padding: 8px 12px; border-radius: 6px; }
</style>
