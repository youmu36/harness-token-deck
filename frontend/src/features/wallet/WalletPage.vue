<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Plus } from 'lucide-vue-next';
import { useWallet } from './useWallet';
import type { CommandData } from '../../app/ports';
import type { Vendor, WalletAction } from './types';
import VendorCard from './VendorCard.vue';
import WalletDialog from './WalletDialog.vue';
import AccountAdjustmentDialog from './AccountAdjustmentDialog.vue';
import AccountEntries from './AccountEntries.vue';
import LocalWalletDialog from './LocalWalletDialog.vue';
import WalletDailyBook from './WalletDailyBook.vue';
import RemoteBalanceRow from './RemoteBalanceRow.vue';
import VendorSettings from './VendorSettings.vue';
import RemoteAccountDialog from './RemoteAccountDialog.vue';
import ProviderScanDialog from './ProviderScanDialog.vue';
import type { RemoteObservation } from './remote';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import SectionNav from '../../ui/SectionNav.vue';
import ManagementLayout from '../../ui/ManagementLayout.vue';
import Money from '../../ui/Money.vue';
import EmptyState from '../../ui/EmptyState.vue';
import { useCommand } from '../../shared/command';
const props = defineProps<{ detail?: string; initialPreferences: CommandData<'preferences.get'> }>();
const wallet = useWallet(props.initialPreferences); const action = ref<WalletAction>(); const vendor = ref<Vendor>();
const remoteVendor = ref<Vendor>(); const remoteObservation = ref<RemoteObservation>();
const scanningProviders = ref(false);
const walletScope = ref<'local'|'remote'>(wallet.preferences.snapshot.value.preferences.walletDefaultScope);
const defaultScope = computed(() => wallet.preferences.snapshot.value.preferences.walletDefaultScope);
const savingDefault = ref(false);
const defaultError = ref('');
async function setDefaultScope() {
  if (savingDefault.value) return;
  savingDefault.value = true; defaultError.value = '';
  try {
    await wallet.preferences.save({ walletDefaultScope: walletScope.value });
  } catch { defaultError.value = wallet.preferences.error.value; }
  finally { savingDefault.value = false; }
}
const localEditing = ref(false);
const adjustment = ref<{vendor:Vendor;target:'top_up'|'consumption'}>();
const selectedId = ref(props.detail ?? '');
const list = ref('active');
const section = ref(props.detail ? 'entries' : 'summary');
const current = computed(() => wallet.query.data.value?.vendors.find(v => v.id === selectedId.value));
const visibilityCommand = useCommand('wallet.setVisibility');
const walletTabs = computed(() => [
  {value:'local',label:'本地钱包'}, {value:'remote',label:'远程钱包'},
].filter(item => !current.value?.walletVisibility || current.value.walletVisibility === 'both' || current.value.walletVisibility === item.value));
watch([() => current.value?.id, () => current.value?.walletVisibility], () => {
  if (!walletTabs.value.some(item => item.value === walletScope.value)) walletScope.value = walletTabs.value[0]!.value as 'local'|'remote';
}, {flush:'sync'});
watch([walletScope,walletTabs],()=>{
  if (!walletTabs.value.some(item=>item.value===walletScope.value)) walletScope.value=walletTabs.value[0]!.value as 'local'|'remote';
}, {flush:'sync'});
async function changeVisibility(event: Event) {
  if (!current.value) return;
  const input = event.target as HTMLSelectElement;
  const mode = input.value as 'local'|'remote'|'both';
  try {await visibilityCommand.run({vendorId:current.value.id,mode});await wallet.query.refresh();}
  catch {input.value = current.value.walletVisibility ?? 'both';}
}
function displayBalance(item: Vendor) {
  const scope = item.walletVisibility === 'remote' || item.walletVisibility === 'local' ? item.walletVisibility : walletScope.value;
  return scope === 'local' ? item.localWallet?.B ?? item.B : item.remoteWallet?.B;
}
const visible = computed(() => list.value === 'archived' ? wallet.archived.value : wallet.active.value);
const sections = computed(() => current.value?.status === 'archived'
  ? [{ value: 'summary', label: '钱包概览' }, { value: 'entries', label: '资金流水' }, {value:'book',label:'记账本'}, { value: 'settings', label: '供应商设置' }]
  : [{ value: 'summary', label: '钱包概览' }, { value: 'entries', label: '资金流水' }, {value:'book',label:'记账本'}, ...(walletScope.value === 'remote' ? [{ value: 'remote', label: '远程账户' }] : []), { value: 'settings', label: '供应商设置' }]);
watch(sections, values => {if(!values.some(item=>item.value===section.value)) section.value='summary';});
watch(() => props.detail, id => { selectedId.value = id ?? ''; walletScope.value = defaultScope.value; section.value = id ? 'entries' : 'summary'; });
watch([wallet.query.data, list, () => props.detail], () => {
  if (!wallet.query.data.value) return;
  if (props.detail && current.value) list.value = current.value.status;
  if (!props.detail && !visible.value.some(v => v.id === selectedId.value)) selectedId.value = visible.value[0]?.id ?? '';
  if (!sections.value.some(item => item.value === section.value)) section.value = 'summary';
});
function selectVendor(id: string) { selectedId.value = id; walletScope.value = (current.value?.walletVisibility === 'local' || current.value?.walletVisibility === 'remote') ? current.value.walletVisibility : defaultScope.value; section.value = 'summary'; }
async function scannedProviders(id: string) { await wallet.query.refresh(); list.value = 'active'; selectVendor(id); }
function open(value: WalletAction, selected?: Vendor) { remoteObservation.value = undefined; action.value = value; vendor.value = selected; }
function calibrateRemote(value: RemoteObservation, selected: Vendor) { open('calibrate', selected); remoteObservation.value = value; }
async function saved(id: string) {
  const intent = action.value;
  await Promise.all([wallet.query.refresh(), wallet.catalog.refresh()]);
  if (intent === 'create' || intent === 'restore') { list.value = 'active'; selectVendor(id); }
}
</script>
<template>
  <div class="wallet-page"><header><div><p class="breadcrumb">管理 / 供应商</p><h1>{{ detail ? '供应商钱包' : '供应商与钱包' }}</h1></div><div class="header-actions"><Button @click="scanningProviders = true">查找最近使用的供应商</Button><Button variant="primary" @click="open('create')"><Plus :size="16" />添加供应商</Button></div></header>
    <InlineAlert v-if="wallet.query.error.value || wallet.catalog.error.value" severity="danger" retry @retry="wallet.query.refresh(); wallet.catalog.refresh()">{{ wallet.query.error.value?.message || wallet.catalog.error.value?.message }}</InlineAlert>
    <p v-if="wallet.query.loading.value" class="text-muted">正在读取供应商…</p>
    <a v-if="detail" href="#/wallet">返回供应商列表</a>
    <ManagementLayout v-if="wallet.query.data.value">
      <template #rail>
        <div class="vendor-list surface">
          <SectionNav v-if="!detail" v-model="list" label="供应商分类" :items="[{ value: 'active', label: '使用中', count: wallet.active.value.length }, { value: 'archived', label: '已归档', count: wallet.archived.value.length }]" />
          <nav aria-label="供应商列表"><button v-for="item in (detail ? wallet.query.data.value.vendors : visible)" :key="item.id" type="button" :aria-pressed="selectedId === item.id" @click="selectVendor(item.id)"><strong>{{ item.name }}</strong><span>{{ item.status === 'archived' ? '已归档' : '当前余额' }} <Money v-if="displayBalance(item) != null" :pico="displayBalance(item)!" mode="trunc2" /><template v-else>—</template></span></button></nav>
          <p v-if="!visible.length && !detail" class="text-muted">{{ list === 'archived' ? '暂无归档记录' : '暂无供应商' }}</p>
        </div>
      </template>
      <template v-if="current">
        <div class="vendor-heading"><div><h2>{{ current.name }}</h2><span class="text-muted">{{ current.status === 'archived' ? '归档记录 · 只读' : 'USD' }}</span></div><div v-if="current.status === 'active'" class="vendor-links"><span>价格组：{{ current.linkedGroups.map(id => wallet.catalog.data.value?.groups.find(g => g.id === id)?.name).filter(Boolean).join('、') || '未关联' }}</span><Button @click="open('link', current)">关联价格组</Button></div></div>
        <div class="wallet-controls"><div class="wallet-choice"><SectionNav v-model="walletScope" label="钱包分类" :items="walletTabs" /><Button :disabled="walletScope === defaultScope" :pending="savingDefault" @click="setDefaultScope">{{walletScope === defaultScope ? '已设为默认钱包' : '设为默认钱包'}}</Button></div><div class="wallet-visibility"><label>显示钱包<select :value="current.walletVisibility ?? 'both'" aria-label="显示钱包" title="隐藏后保留原账目，可随时恢复显示" :disabled="visibilityCommand.pending.value" @change="changeVisibility"><option value="both">本地与远程</option><option value="local">仅本地钱包</option><option value="remote">仅远程钱包</option></select></label></div></div>
        <InlineAlert v-if="visibilityCommand.error.value" severity="danger">{{visibilityCommand.error.value.message}}</InlineAlert>
        <p class="wallet-source text-muted">{{walletScope === 'local' ? '本地钱包 · 人工记录充值，按本地估算消费记账。' : '远程钱包 · 按远程账户余额变动核算充值与消费。'}} 金额校准仅作用于当前钱包。</p>
        <InlineAlert v-if="defaultError" severity="danger">{{defaultError}}</InlineAlert>
        <SectionNav :model-value="section" label="供应商二级导航" :items="sections" @update:model-value="section = $event" />
        <VendorCard v-if="section === 'summary' && (walletScope === 'local' || current.remoteWallet)" :vendor="current" :scope="walletScope" :catalog="wallet.catalog.data.value" :mode="wallet.mode.value" @local="localEditing = true" @action="open($event, current)" @entries="section = 'entries'" @correct="$event === 'top_up' ? section = 'entries' : adjustment = { vendor: current, target: $event }" />
        <section v-else-if="section === 'summary'" class="remote-panel surface"><EmptyState message="尚无远程钱包记录，请先配置远程账户。" /><Button v-if="current.status === 'active'" @click="remoteVendor=current">配置远程账户</Button></section><AccountEntries v-else-if="section === 'entries'" :key="`${walletScope}/${current.id}`" :vendor-id="current.id" :mode="wallet.mode.value" :scope="walletScope" :readonly="current.status === 'archived'" /><WalletDailyBook v-else-if="section === 'book'" :key="`${walletScope}/${current.id}`" :vendor-id="current.id" :scope="walletScope" />
        <section v-else-if="section === 'remote'" class="remote-panel surface"><h2>远程账户与余额</h2><p class="text-muted">远程余额按观测时点显示。支持累计消费时优先核算区间消费与精确净流入；只有余额时结合本地估算推算，候选净流入至少 1 USD 才向上取整记充值。首次观测只建基线，区间充值可包含多笔、赠送或退款。</p><RemoteBalanceRow :key="current.id" :vendor-id="current.id" :mode="wallet.mode.value" @configure="remoteVendor = current" @calibrate="calibrateRemote($event, current)" /><Button @click="remoteVendor = current">编辑远程账户</Button></section>
        <VendorSettings v-else :vendor="current" :scope="walletScope" @local="localEditing = true" @action="open($event, current)" @correct="walletScope === 'local' ? localEditing = true : $event === 'top_up' ? section = 'entries' : adjustment = { vendor: current, target: $event }" />
      </template>
      <EmptyState v-else :message="detail ? '供应商不存在' : list === 'archived' ? '暂无归档记录' : '添加供应商后可记录充值与余额'" />
    </ManagementLayout>
    <RemoteAccountDialog v-if="remoteVendor" :key="remoteVendor.id" :vendor-id="remoteVendor.id" :vendor-name="remoteVendor.name" :vendor-url="remoteVendor.url" @close="remoteVendor = undefined" @saved="wallet.query.refresh()" />
    <LocalWalletDialog v-if="localEditing && current" :key="current.id" :vendor="current" @close="localEditing = false" @saved="wallet.query.refresh()" />
    <AccountAdjustmentDialog v-if="adjustment" :scope="walletScope" :vendor="adjustment.vendor" :target="adjustment.target" :mode="wallet.mode.value" @close="adjustment = undefined" @saved="wallet.query.refresh()" />
    <WalletDialog v-if="action" :wallet-scope="walletScope" :hide-past="walletScope === 'local' && action === 'rename'" :key="`${action}/${vendor?.id}`" :action="action" :vendor="vendor" :catalog="wallet.catalog.data.value" :remote-observation="remoteObservation" :wallet-revision="wallet.query.data.value?.revision ?? 0" :refresh="wallet.query.refresh" :latest="() => wallet.query.data.value?.vendors.find(v => v.id === vendor?.id)" @close="action = undefined" @saved="saved" />
  <ProviderScanDialog v-if="scanningProviders" @close="scanningProviders = false" @saved="scannedProviders" /></div>
</template>
<style scoped>
.wallet-choice {display:flex;align-items:center;flex-wrap:wrap;gap:12px;}
.wallet-controls{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}.vendor-heading > div:first-child{display:flex;align-items:baseline;gap:12px}.vendor-heading span{font-size:12px;}.wallet-page :deep(.management-workspace){gap:14px;}
.wallet-visibility,.wallet-visibility label{display:flex;align-items:center;flex-wrap:wrap;gap:10px;font-size:12px}.wallet-visibility select{padding:7px 10px;}
.wallet-source {font-size:13px;line-height:1.7;border-left:3px solid var(--accent);padding-left:12px;}
.wallet-page { display: grid; gap: 24px; }.wallet-page > * { min-width: 0; }
header, .vendor-heading { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }.header-actions { display: flex; flex-wrap: wrap; gap: 10px; }header button { display: inline-flex; align-items: center; gap: 6px; }
.breadcrumb { color: var(--text-muted); font-size: 12px; margin-bottom: 12px; }.page-intro { font-size: 13px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }
.vendor-list { padding: 8px; }.vendor-list nav[aria-label='供应商列表'] { display: grid; gap: 6px; padding-top: 12px; }
.vendor-list :deep(.section-nav button){padding:10px 8px;}
.vendor-list nav[aria-label='供应商列表'] button { display: grid; gap: 6px; text-align: left; min-width: 0; padding: 14px 12px; border: 1px solid transparent; border-radius: 6px; background: transparent; }
.vendor-list nav[aria-label='供应商列表'] button[aria-pressed='true'] { border-color: var(--accent); background: var(--accent-soft); }.vendor-list button:hover { background: var(--surface-2); }
.vendor-list strong { overflow-wrap: anywhere; }.vendor-list button span, .vendor-list > p, .vendor-heading > span { color: var(--text-muted); font-size: 12px; }.vendor-list > p { padding: 16px; }
.vendor-links { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; font-size: 13px; }.remote-panel { display: grid; gap: 16px; padding: 24px; }.remote-panel > button { justify-self: start; }.remote-panel p { font-size: 13px; }
</style>
