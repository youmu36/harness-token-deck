<script setup lang="ts">
import { computed, inject, ref, type DeepReadonly } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { unwrap, asAppError } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { formatDateTime } from '../../shared/format';
import { harnessNames } from '../harness/types';
import Button from '../../ui/Button.vue';
import Badge from '../../ui/Badge.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import EmptyState from '../../ui/EmptyState.vue';
import Money from '../../ui/Money.vue';
import UnpricedResolutionDialog from './UnpricedResolutionDialog.vue';
const emit = defineEmits<{ edit: [groupId: string, versionId: string] }>();
const bridge = inject(BridgeKey)!;
const query = useQuery('pricing.unpriced', () => bridge.call('pricing.unpriced', {}).then(unwrap), { events: ['usage.changed', 'pricing.changed', 'quality.changed'], coalesce: true });
const preferences = useQuery('unpriced.preferences', () => bridge.call('preferences.get', {}).then(unwrap), { events: ['preferences.changed'] });
const mode = computed(() => preferences.data.value?.preferences.timezone ?? 'system');
const command = useCommand('pricing.recalculateUnpriced');
const result = ref<CommandData<'pricing.recalculateUnpriced'>>();
const error = ref('');
const search = ref('');
const showIgnored = ref(false); const selected = ref<string[]>([]);
const resolve = useCommand('pricing.resolveUnpriced');
type Item = DeepReadonly<CommandData<'pricing.unpriced'>['items'][number]>;
const editing = ref<{ item: Item; action: 'price' | 'assign' | 'ignore' }>();
const rows = computed(() => query.data.value?.items.filter(item => (showIgnored.value || !item.ignored) && `${item.modelId ?? ''} ${item.groupName} ${item.versionName}`.toLowerCase().includes(search.value.trim().toLowerCase())) ?? []);
const selectedIds = computed(() => rows.value.filter(item => !item.ignored && selected.value.includes(item.id)).map(item => item.id));
async function resolved() { selected.value = []; result.value = undefined; await query.refresh(); }
async function ignoreSelection(item?: Item) {
  if (!query.data.value) return;
  error.value = '';
  const ids = item ? [item.id] : selectedIds.value; if (!ids.length) return;
  try { await resolve.run({ itemIds: ids as [string, ...string[]], action: item ? 'restore' : 'ignore', expectedPricingRevision: query.data.value.pricingRevision, expectedUsageRevision: query.data.value.usageRevision }); await resolved(); }
  catch (failure) { error.value = asAppError(failure).message; await query.refresh(); }
}
async function recalculate() {
  if (!query.data.value || command.pending.value) return;
  error.value = ''; result.value = undefined;
  try { result.value = await command.run({ expectedPricingRevision: query.data.value.pricingRevision }); await query.refresh(); }
  catch (failure) { error.value = asAppError(failure).message; if (asAppError(failure).code === 'PRICING_REVISION_CONFLICT') await query.refresh(); }
}
</script>
<template>
  <section class="unpriced-tab" aria-label="未定价模型相关">
    <header><div><h2>补全遗漏的模型价格</h2><p class="text-muted">最近 30 天 · 支持独立补价、归组与忽略</p></div><div class="actions"><Button :disabled="command.pending.value" :pending="query.refreshing.value" @click="query.refresh">刷新列表</Button><Button variant="primary" :disabled="!query.data.value?.readyCount || query.refreshing.value || resolve.pending.value" :pending="command.pending.value" @click="recalculate">一键重算已补价请求<span v-if="query.data.value?.readyCount">（{{ query.data.value.readyCount }}）</span></Button></div></header>
    <InlineAlert v-if="query.data.value?.limited" severity="warning">列表最多展示每个 Harness 2500 条保留明细的分组；忽略与改归属只处理当前所选成员。批量补价会冻结完整待处理集合，每批最多 500 条。</InlineAlert>
    <p class="instructions">可直接编辑模型价格，或选择已有价格组与版本。丢失归属的请求可放入历史价格组，仅用于统计分析。保存价格后，一键重算仍未定价且未忽略的请求；超过 30 天的历史不再重算。</p>
    <InlineAlert v-if="query.error.value || error" severity="danger" retry @retry="query.refresh">{{ error || query.error.value?.message }}</InlineAlert>
    <InlineAlert v-if="result" :severity="result.complete ? 'success' : 'warning'">已重算 {{ result.recalculated }} 条，费用差额 <Money :pico="result.costDeltaPico" mode="usd4" />；仍有 {{ result.remaining }} 条待补价或需处理归属。<span v-if="!result.complete">本次仅完成部分批次（{{ result.errorCode }}），重试会继续已冻结的请求。</span></InlineAlert>
    <div class="list-tools"><label>搜索<input v-model="search" type="search" aria-label="搜索未定价模型" placeholder="模型、价格组或版本" /></label><label class="check"><input v-model="showIgnored" type="checkbox" />显示已忽略（{{ query.data.value?.ignoredCount ?? 0 }}）</label><Button :disabled="!selectedIds.length || command.pending.value || resolve.pending.value" :pending="resolve.pending.value" @click="ignoreSelection()">忽略选中（{{ selectedIds.length }}）</Button><span class="text-muted">{{ query.data.value?.recordCount ?? 0 }} 条未定价请求 · {{ query.data.value?.readyCount ?? 0 }} 条可重算</span></div>
    <p v-if="query.loading.value" role="status">正在读取未定价模型…</p>
    <div v-else-if="rows.length" class="table-wrap" tabindex="0" aria-label="未定价模型表"><table><thead><tr><th><input type="checkbox" aria-label="全选当前未定价模型" :checked="rows.filter(r => !r.ignored).length > 0 && selectedIds.length === rows.filter(r => !r.ignored).length" @change="selected = ($event.target as HTMLInputElement).checked ? rows.filter(r => !r.ignored).map(r => r.id) : []" /></th><th>模型 / 来源</th><th>价格组 / 版本</th><th>请求</th><th>需处理内容</th><th>操作</th></tr></thead><tbody><tr v-for="item in rows" :key="item.id || JSON.stringify([item.harness, item.modelId, item.groupId, item.versionId])"><td><input v-model="selected" type="checkbox" :value="item.id" :aria-label="`选择 ${item.modelId ?? '未知模型'} ${item.versionName}`" :disabled="item.ignored || command.pending.value || resolve.pending.value" /></td><td><strong>{{ item.modelId ?? '未知模型' }}</strong><small>{{ harnessNames[item.harness] }}</small><small>{{ formatDateTime(item.firstAt, mode) }} 至 {{ formatDateTime(item.lastAt, mode) }}</small></td><td>{{ item.groupName }}<small>{{ item.versionName }}</small></td><td class="count">{{ item.recordCount }}<small v-if="item.readyCount">可重算 {{ item.readyCount }}</small></td><td><Badge v-if="item.ignored">已忽略</Badge><Badge v-else-if="item.readyCount === item.recordCount" tone="success">价格已补齐</Badge><p v-for="reason in item.reasons" :key="reason">{{ reason }}</p></td><td><div class="row-actions"><Button :disabled="command.pending.value || resolve.pending.value" @click="editing = { item, action: 'price' }">编辑价格</Button><Button :disabled="command.pending.value || resolve.pending.value" @click="editing = { item, action: 'assign' }">编辑价格组 / 版本</Button><Button v-if="item.ignored" :pending="resolve.pending.value" @click="ignoreSelection(item)">取消忽略</Button><Button v-else :disabled="command.pending.value || resolve.pending.value" @click="editing = { item, action: 'ignore' }">忽略…</Button><Button v-if="item.groupId && item.versionId && item.groupId !== 'htu-history'" :disabled="command.pending.value" @click="emit('edit', item.groupId, item.versionId)">前往补价</Button></div></td></tr></tbody></table></div>
    <EmptyState v-else :message="search ? '没有匹配的未定价模型' : '最近 30 天没有待补价的请求明细'" />
    <aside v-if="query.data.value?.archived.length" class="archived"><h3>升级前已汇总的未定价用量</h3><p class="text-muted">以下数据已缺少逐请求计费条件和原价格组/版本，无法精确重算。不会以当前价格推测历史费用。</p><ul><li v-for="item in query.data.value.archived" :key="`${item.harness}/${item.modelId}`">{{ item.modelId ?? '未知模型' }} · {{ harnessNames[item.harness] }} · {{ item.buckets }} 个汇总桶</li></ul></aside>
    <UnpricedResolutionDialog v-if="editing && query.data.value" :item="editing.item" :action="editing.action" :pricing-revision="query.data.value.pricingRevision" :usage-revision="query.data.value.usageRevision" @close="editing = undefined" @saved="resolved" @refresh="query.refresh" />
  </section>
</template>
<style scoped>
.unpriced-tab { display: grid; gap: 20px; min-width: 0; padding: 24px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; }header, .actions, .list-tools { display: flex; align-items: center; flex-wrap: wrap; justify-content: space-between; gap: 12px; }header h2 { margin-bottom: 6px; font-size: 18px; }.actions { justify-content: flex-end; }.instructions { max-width: 900px; line-height: 1.8; font-size: 13px; }.list-tools { font-size: 12px; }.list-tools label { display: flex; align-items: center; gap: 10px; }.table-wrap { overflow: auto; border: 1px solid var(--border); border-radius: 6px; }table { width: 100%; min-width: 760px; border-collapse: collapse; background: var(--surface); }th, td { padding: 14px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }th { font-size: 12px; background: var(--surface-2); }td { font-size: 13px; }.row-actions { display: flex; flex-wrap: wrap; gap: 6px; min-width: 210px; }.check { display: flex; align-items: center; gap: 6px; }td:nth-child(2) { max-width: 290px; overflow-wrap: anywhere; }td p + p { margin-top: 5px; }small { display: block; margin-top: 5px; font-size: 11px; color: var(--text-muted); }strong { font-weight: 600; }.count { font-variant-numeric: tabular-nums; white-space: nowrap; }.archived { display: grid; gap: 10px; border-top: 1px solid var(--border); padding-top: 20px; font-size: 13px; }.archived ul { padding-left: 20px; line-height: 1.8; }
</style>
