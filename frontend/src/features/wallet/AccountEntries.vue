<script setup lang="ts">
import { computed, inject, ref, shallowRef, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { unwrap, asAppError } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import Money from '../../ui/Money.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import FlowCorrectionDialog from './FlowCorrectionDialog.vue';
const props = defineProps<{vendorId:string;mode:TimeZoneMode;scope?:'local'|'remote';readonly?:boolean}>();
type Entry = CommandData<'wallet.accountEntries'>['entries'][number];
const bridge = inject(BridgeKey)!;
const kind = ref<'all'|'top_up'|'consumption'|'adjustment'>('all');
const startDay = ref('');
const endDay = ref('');
const invalidRange = computed(() => !!startDay.value && !!endDay.value && startDay.value > endDay.value);
const request = (): RequestMap['wallet.accountEntries'] => ({vendorId:props.vendorId,scope:props.scope,
  limit:50,kind:kind.value,startDay:startDay.value || null,endDay:endDay.value || null});
const query = useQuery('wallet.accountEntries', async () => {
  if (invalidRange.value) throw new Error('结束日期不能早于开始日期');
  return {...unwrap(await bridge.call('wallet.accountEntries',request())),updatedAt:new Date().toISOString()};
}, {deps:[()=>props.vendorId,()=>props.scope,kind,startDay,endDay],clearOnChange:true,
  events:[{name:'wallet.changed',when:event=>event.vendorIds.includes(props.vendorId)}],
  coalesce:true,eventIntervalMs:3*3600_000});
const rows = shallowRef<Entry[]>([]);
const cursor = ref<string|null>();
const error = ref('');
const pending = ref(false);
const editing = shallowRef<Entry>();
const dismissal = useCommand('wallet.dismissNotice');
async function dismiss(row:Entry) {
  if(!row.noticeId || dismissal.pending.value)return;
  try {await dismissal.run({vendorId:props.vendorId,entryId:row.id,noticeId:row.noticeId});await query.refresh();}
  catch(failure){error.value=asAppError(failure).message;}
}
let generation = 0;
watch([kind,startDay,endDay,()=>props.vendorId,()=>props.scope],()=>{generation++;rows.value=[];cursor.value=null;pending.value=false;});
watch(query.data,data=>{generation++;rows.value=data?.entries ?? [];cursor.value=data?.nextCursor;pending.value=false;error.value='';});
async function more() {
  if (!cursor.value || pending.value || invalidRange.value) return;
  const current = generation;
  pending.value = true; error.value = '';
  try {
    const data = unwrap(await bridge.call('wallet.accountEntries',{...request(),cursor:cursor.value}));
    if (current !== generation) return;
    if (data.revision !== query.data.value?.revision) {await query.refresh();return;}
    const ids = new Set(rows.value.map(row=>row.id));
    rows.value = [...rows.value,...data.entries.filter(row=>!ids.has(row.id))].slice(-200);
    cursor.value = data.nextCursor;
  } catch (failure) {if (current === generation) error.value = asAppError(failure).message;}
  finally {if (current === generation) pending.value=false;}
}
const typeLabel = (row:Entry) => row.kind === 'top_up' || row.kind === 'manual_top_up' ? '充值' : row.kind === 'consumption' ? '消费' : '调整与校准';
</script>
<template>
  <section class="account-entries surface">
    <header><div><h2>{{scope === 'local' ? '本地钱包' : '远程钱包'}}资金流水</h2><p class="text-muted">保留最近 30 天；较早的每日充值与消费可在记账本查看。</p></div><Button :pending="query.loading.value || query.refreshing.value" @click="query.refresh">更新流水</Button></header>
    <p class="text-muted">{{scope === 'local' ? '充值来自人工记录，消费来自本地估算。' : '充值与消费根据远程账户余额变化核算。'}} 本地数据至少十分钟没有新增且计费完成后，合并生成流水。金额校准只影响当前钱包。</p>
    <div class="filters"><label>日期从<input v-model="startDay" type="date" aria-label="流水开始日期" /></label><label>至<input v-model="endDay" type="date" aria-label="流水结束日期" /></label><label>类型<select v-model="kind" aria-label="流水类型"><option value="all">全部类型</option><option value="consumption">消费</option><option value="top_up">充值</option><option value="adjustment">调整与校准</option></select></label></div>
    <div class="book-status text-muted">上次更新：{{query.data.value ? formatDateTime(query.data.value.updatedAt,mode) : '—'}}</div>
    <InlineAlert v-if="query.error.value || error" severity="danger">{{error || query.error.value?.message}}</InlineAlert>
    <div class="table-wrap" tabindex="0" :aria-label="`${scope === 'local' ? '本地' : '远程'}钱包资金流水表`"><table><thead><tr><th>时间</th><th>类型</th><th>本次金额</th><th>金额变化（前 → 后）</th><th>备注</th><th v-if="!readonly">操作</th></tr></thead><tbody>
      <tr v-for="row in rows" :key="row.id"><td>{{formatDateTime(row.occurredAt,mode)}}<small v-if="row.postedAt">生成于 {{formatDateTime(row.postedAt,mode)}}</small></td><td>{{typeLabel(row)}}</td><td><template v-if="row.kind === 'top_up'"><Money :pico="row.R" /></template><template v-else-if="row.kind === 'consumption'"><Money :pico="row.C" /></template><template v-else><span v-if="row.R !== '0'">充值 <Money :pico="row.R" /></span><span v-if="row.C !== '0'">消费 <Money :pico="row.C" /></span><span v-if="row.A !== '0'">校准项 <Money :pico="row.A" /></span><span v-if="row.R === '0' && row.C === '0' && row.A === '0'">金额未变 <Money pico="0" /></span></template></td>
      <td class="changes"><span v-if="row.beforeR != null && row.afterR != null && row.R !== '0'">累计充值 <Money :pico="row.beforeR" /> → <Money :pico="row.afterR" /></span><span v-if="row.beforeC != null && row.afterC != null && (row.C !== '0' || row.kind === 'adjustment')">累计消费 <Money :pico="row.beforeC" /> → <Money :pico="row.afterC" /></span><span v-if="row.beforeB != null && row.afterB != null">余额 <Money :pico="row.beforeB" /> → <Money :pico="row.afterB" /></span></td><td class="note">{{row.note || '—'}}<small v-if="row.manuallyCalibrated">已人工校准</small><div v-if="row.noticeId" class="estimate-notice"><p>钱包已修正，估算与记账仍有差额。可核对价格后重算相关历史；手动校准金额会保留。</p><p v-if="row.estimatedConsumptionPico != null">估算依据 <Money :pico="row.estimatedConsumptionPico" />，记账 <Money :pico="row.C" />。</p><Button aria-label="关闭估算核对提醒" :pending="dismissal.pending.value" @click="dismiss(row)">关闭提醒</Button></div></td><td v-if="!readonly"><Button v-if="scope && ['top_up','consumption'].includes(row.kind)" @click="editing=row">校准金额</Button></td></tr>
      <tr v-if="!rows.length"><td :colspan="readonly ? 5 : 6">{{query.loading.value ? '正在读取…' : '所选日期和类型暂无流水'}}</td></tr>
    </tbody></table></div>
    <Button v-if="cursor" :pending="pending" @click="more">加载更早流水</Button>
    <FlowCorrectionDialog v-if="editing && scope" :vendor-id="vendorId" :scope="scope" :entry="editing" @close="editing=undefined" @saved="query.refresh" />
  </section>
</template>
<style scoped>
.estimate-notice{margin-top:10px;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2)}.estimate-notice p+p{margin-top:6px}.estimate-notice button{margin-top:8px}
.account-entries{padding:20px;display:grid;gap:14px;min-width:0}header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}header p{margin-top:6px}.filters{display:flex;align-items:end;flex-wrap:wrap;gap:12px;padding:12px 0;border-block:1px solid var(--border)}label{display:grid;gap:6px;font-size:12px}input,select{padding:7px 10px;min-width:145px}.book-status{font-size:12px}.table-wrap{overflow:auto;height:clamp(240px,45vh,480px);overflow-anchor:none;border:1px solid var(--border);border-radius:6px}table{border-collapse:collapse;width:100%;background:var(--surface)}th,td{padding:12px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;vertical-align:top}th{position:sticky;top:0;background:var(--surface-2)}td > span,small{display:block}small{font-size:11px;color:var(--text-muted);margin-top:6px}.changes > span+span{margin-top:6px}.note{white-space:normal;min-width:160px;max-width:320px;overflow-wrap:anywhere}p{font-size:12px;line-height:1.7}
</style>
