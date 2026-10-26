<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { readAnalysis } from '../overview/analysisQuery';
import { formatDateTimeInput, parseDateTimeInput, type TimeZoneMode } from '../../shared/time';
import Button from '../../ui/Button.vue';
import DateTimeField from '../../ui/DateTimeField.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Money from '../../ui/Money.vue';

const props = defineProps<{ vendorId: string; mode: TimeZoneMode; accounts?: boolean }>();
const bridge = inject(BridgeKey)!;
const end = new Date(), start = new Date(end.getTime() - 30 * 86400000);
const startText = ref(formatDateTimeInput(start, props.mode)), endText = ref(formatDateTimeInput(end, props.mode));
const startField = ref<InstanceType<typeof DateTimeField>>(), endField = ref<InstanceType<typeof DateTimeField>>();
const range = ref({ start: start.toISOString(), end: end.toISOString() });
const source = ref(''), model = ref(''), group = ref(''), reason = ref('');
const status = ref<'all' | 'unacknowledged' | 'acknowledged'>('unacknowledged');
const offset = ref(0), correctionOffset = ref(0), error = ref(''), pending = ref(false);
const search = ref(''), modelOffset = ref(0);
const options = useQuery('wallet.evidence.options', () => bridge.call('stats.exploreOptions', { modelSearch: search.value, modelOffset: modelOffset.value, modelLimit: 100 }).then(unwrap), {
  visible: () => !props.accounts, deps: [search, modelOffset], clearOnChange: true, events: ['pricing.changed'],
});
watch(search, () => { modelOffset.value = 0; });
const applied = ref<RequestMap['stats.explore']>({ ...range.value, vendorIds: [props.vendorId], grain: 'day', qualityStatus: status.value, sections: props.accounts ? ['accounts'] : ['quality'], costBasis: props.accounts ? 'remote' : 'local' });
watch(() => props.mode, () => { startText.value = formatDateTimeInput(new Date(range.value.start), props.mode); endText.value = formatDateTimeInput(new Date(range.value.end), props.mode); });
function apply() {
  try {
    if (!startField.value?.commit() || !endField.value?.commit()) throw new RangeError('请检查起止日期');
    const from = startText.value === formatDateTimeInput(new Date(range.value.start), props.mode) ? range.value.start : parseDateTimeInput(startText.value, props.mode).toISOString();
    const to = endText.value === formatDateTimeInput(new Date(range.value.end), props.mode) ? range.value.end : parseDateTimeInput(endText.value, props.mode).toISOString();
    if (from >= to) throw new RangeError('结束时间须晚于开始时间');
    range.value = { start: from, end: to }; offset.value = correctionOffset.value = 0;
    applied.value = { ...range.value, vendorIds: [props.vendorId], grain: 'day', sections: props.accounts ? ['accounts'] : ['quality'],
      costBasis: props.accounts ? 'remote' : 'local', qualityStatus: status.value,
      qualityReasons: reason.value ? [reason.value as 'cache'] : [], harnesses: source.value ? [source.value as 'codex'] : [],
      groupIds: group.value ? [group.value] : [], modelKeys: model.value ? [model.value] : [] };
    error.value = '';
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '请检查筛选条件'; }
}
const parameters = computed(() => ({ ...applied.value, qualityOffset: offset.value, accountOffset: offset.value, accountCorrectionOffset: correctionOffset.value }));
const query = useQuery('wallet.evidence', signal => readAnalysis(bridge, parameters.value, signal), {
  deps: [() => JSON.stringify(parameters.value)], clearOnChange: true, coalesce: true, eventIntervalMs: 60000,
  events: [props.accounts ? 'wallet.changed' : 'quality.changed', 'usage.changed'],
});
async function acknowledge(item: CommandData<'stats.explore'>['quality'][number]) {
  if (pending.value) return;
  pending.value = true; error.value = '';
  try {
    const result = unwrap(await bridge.call('stats.acknowledgeQuality', { items: [{ id: item.id, fingerprint: item.fingerprint, revision: query.data.value?.revision }] }));
    if (result.conflicts) error.value = '这组缺失已有新证据，请重新核对后确认。';
    await query.refresh();
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '确认失败，请重试'; }
  finally { pending.value = false; }
}
const names: Record<string, string> = { claude: 'Claude Code', codex: 'Codex', zcode: 'Zcode', dsh: 'Deepseek Harness' };
const methods: Record<string, string> = { remote_usage: '可靠累计消费增量', balance_estimate: '余额与本地估算推算', balance_unresolved: '余额推算，差额待核实', legacy_local: '旧版本地估算', local_estimate: '本地估算' };
const statuses = { matched: '误差范围内', review: '待核对', estimated: '推算参考', collecting: '尚未稳定', coverage_incomplete: '覆盖不足' };
const corrections: Record<string, string> = { consumption_correction: '累计消费更正', balance_correction: '余额校准', correction: '流水更正' };
const groupName = (id: string) => options.data.value?.groups.find(item => item.id === id)?.name ?? (id ? '历史价格组' : '信息缺失');
</script>

<template>
  <section class="wallet-evidence" :aria-label="accounts ? '远程消费对照' : vendorId ? '供应商缺失记录' : '未归属记录'">
    <header><h2>{{ accounts ? '远程消费对照' : vendorId ? '缺失记录与当前证据' : '未归属记录' }}</h2><Button :pending="query.refreshing.value" @click="query.refresh">更新记录</Button></header>
    <p v-if="accounts">账户范围的远程消费只供核对；余额推算不表示独立实扣，也不证明本地漏采。采用目标请进入消费核对。</p>
    <p v-else>确认只关闭当前证据的提醒，保留缺失原因、Token 与金额。出现新证据会重新提醒。{{ vendorId ? '这里只显示当前供应商记录。' : '这些记录尚无可确认供应商，不计入任何供应商账目。' }}</p>
    <div class="filters"><DateTimeField ref="startField" v-model="startText" label="记录开始" :mode="mode" /><DateTimeField ref="endField" v-model="endText" label="记录结束" :mode="mode" />
      <template v-if="!accounts"><label>来源<select v-model="source" aria-label="缺失来源"><option value="">全部来源</option><option v-for="(name, id) in names" :key="id" :value="id">{{ name }}</option></select></label><label>价格组<select v-model="group" aria-label="缺失价格组"><option value="">全部价格组</option><option v-for="item in options.data.value?.groups" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
        <label>搜索模型<input v-model="search" aria-label="缺失模型搜索" placeholder="按名称搜索" /></label><label>模型<select v-model="model" aria-label="缺失模型"><option value="">全部模型</option><option v-for="item in options.data.value?.models" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><Button v-if="modelOffset" @click="modelOffset = Math.max(0, modelOffset - 100)">上一页模型</Button><Button v-if="options.data.value?.modelNextOffset != null" @click="modelOffset = options.data.value!.modelNextOffset!">下一页模型</Button>
        <label>原因<select v-model="reason" aria-label="缺失原因"><option value="">全部原因</option><option value="version">价格组/倍率版本缺失</option><option value="cache">缓存覆盖不完整</option><option value="incomplete">用量或计费字段不完整</option><option value="unpriced">未定价</option></select></label><label>确认状态<select v-model="status" aria-label="缺失确认状态"><option value="unacknowledged">未确认</option><option value="acknowledged">已确认</option><option value="all">全部记录</option></select></label></template>
      <Button variant="primary" @click="apply">应用筛选</Button>
    </div>
    <InlineAlert v-if="error || query.error.value || options.error.value" severity="danger">{{ error || query.error.value?.message || options.error.value?.message }}</InlineAlert>
    <p v-if="query.loading.value" class="text-muted">正在读取当前证据…</p>
    <template v-if="query.data.value"><p v-for="item in query.data.value.coverage" :key="item.code" class="text-muted">{{ item.message }}</p>
      <template v-if="!accounts"><p>{{ query.data.value.qualityTotal }} 组 · {{ query.data.value.qualityUnacknowledged }} 组待确认</p><div class="table-wrap"><table aria-label="钱包缺失证据"><thead><tr><th>日期</th><th>来源 / 模型</th><th>价格组</th><th>缺失原因</th><th>请求数</th><th>确认</th></tr></thead><tbody><tr v-for="item in query.data.value.quality" :key="`${item.id}:${item.fingerprint}`"><td>{{ item.date }}</td><td>{{ names[item.harness] ?? item.harness }} / {{ item.displayName }}</td><td>{{ groupName(item.groupId) }}</td><td>{{ item.reasons.join('、') }}</td><td>{{ item.countAccuracy === 'at_least' ? '至少 ' : '' }}{{ BigInt(item.recordCount).toLocaleString('en-US') }}</td><td><span v-if="item.acknowledged">已确认当前证据</span><Button v-else :pending="pending" @click="acknowledge(item)">确认已知缺失</Button></td></tr><tr v-if="!query.data.value.quality.length"><td colspan="6">当前筛选没有记录，可调整时间或确认状态。</td></tr></tbody></table></div>
        <footer><Button v-if="offset" @click="offset = Math.max(0, offset - 100)">上一页记录</Button><Button v-if="query.data.value.qualityNextOffset !== null" @click="offset = query.data.value!.qualityNextOffset!">下一页记录</Button></footer></template>
      <template v-else><p>远程有效消费 <Money mode="full6" :pico="query.data.value.remoteCostPico" />；独立更正 <Money mode="full6" :pico="query.data.value.unallocatedCostPico" /></p><div class="table-wrap"><table aria-label="钱包远程账户区间"><thead><tr><th>区间</th><th>依据</th><th>远程消费</th><th>当前本地估算</th><th>差额 / 状态</th></tr></thead><tbody><tr v-for="item in query.data.value.accounts" :key="item.id"><td>{{ item.start }}<br />{{ item.end }}</td><td>{{ methods[item.method] ?? item.method }}</td><td><Money mode="full6" :pico="item.costPico" /></td><td><Money mode="full6" :pico="item.currentEstimatePico" /></td><td><Money mode="full6" :pico="item.differencePico" /><br />{{ statuses[item.status] }}</td></tr><tr v-if="!query.data.value.accounts.length"><td colspan="5">当前范围没有账户区间。可扩大时间或检查远程配置。</td></tr></tbody></table></div><footer><Button v-if="offset" @click="offset = Math.max(0, offset - 100)">上一页区间</Button><Button v-if="query.data.value.accountNextOffset !== null" @click="offset = query.data.value!.accountNextOffset!">下一页区间</Button></footer>
        <section v-if="query.data.value.accountCorrectionTotal"><h3>未绑定区间的更正</h3><p v-for="item in query.data.value.accountCorrections" :key="item.id">{{ item.occurredAt }} · {{ corrections[item.kind] ?? item.kind }} · <Money mode="full6" :pico="item.costPico" /> · {{ item.note }}</p><footer><Button v-if="correctionOffset" @click="correctionOffset = Math.max(0, correctionOffset - 100)">上一页更正</Button><Button v-if="query.data.value.accountCorrectionNextOffset !== null" @click="correctionOffset = query.data.value!.accountCorrectionNextOffset!">下一页更正</Button></footer></section>
        <a :href="`#/wallet/${encodeURIComponent(vendorId)}/reconcile`">核对本地成员并预览目标</a></template>
    </template>
  </section>
</template>

<style scoped>
.wallet-evidence{display:grid;gap:18px;min-width:0}header,footer,.filters{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}.filters{justify-content:flex-start;align-items:flex-end}.filters label{display:grid;gap:6px;font-size:13px}.filters input,.filters select{padding:9px;max-width:240px}p{font-size:13px;line-height:1.7}h2{font-size:19px}h3{font-size:16px}.table-wrap{max-height:470px;overflow:auto;border:1px solid var(--border);border-radius:var(--radius)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:12px;text-align:left;border-bottom:1px solid var(--border)}th{position:sticky;top:0;background:var(--surface-2);white-space:nowrap}footer{justify-content:flex-end}
</style>
