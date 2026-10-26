<script setup lang="ts">
import { computed, inject, onScopeDispose, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type RequestMap } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { formatDateTimeInput, parseDateTimeInput, type TimeZoneMode } from '../../shared/time';
import { formatDateTime } from '../../shared/format';
import { amountDraft, amountPico, calibrationSession } from './calibrationDraft';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import DateTimeField from '../../ui/DateTimeField.vue';
const props = defineProps<{vendorId:string;mode:TimeZoneMode;readonly?:boolean;manual?:boolean}>();
const bridge = inject(BridgeKey)!;
const session = calibrationSession(bridge, props.vendorId);
const {draft, preview, plan, pending, error, message, uncertain} = session;
const editing = ref(props.manual ?? false), previewing = ref(false), historyOpen = ref(false);
const browse = ref({start:draft.start,end:draft.end}), remoteOffset = ref(0), liveBrowse = ref(true);
const startText = ref(formatDateTimeInput(new Date(draft.start),props.mode)), endText = ref(formatDateTimeInput(new Date(draft.end),props.mode));
const startValid = ref(true), endValid = ref(true);
const startField = ref<InstanceType<typeof DateTimeField>>(), endField = ref<InstanceType<typeof DateTimeField>>();
const valid = computed(() => startValid.value && endValid.value);
const query = computed<RequestMap['reconciliation.preview']>(() => ({vendorId:props.vendorId,start:draft.start,end:draft.end,localBatchIds:[...draft.batchIds],intervalIds:[...draft.intervalIds]}));
const base = useQuery('wallet.calibration.scope',() => bridge.call('reconciliation.preview',query.value).then(unwrap),{visible:editing,deps:[() => JSON.stringify(query.value)],clearOnChange:true,clearOnHide:true,coalesce:true});
const batches = useQuery('wallet.calibration.periods',() => bridge.call('reconciliation.batches',{vendorId:props.vendorId,...browse.value,...(liveBrowse.value ? {end:new Date().toISOString()} : {}),remoteOffset:remoteOffset.value}).then(unwrap),{visible:() => !editing.value,deps:[browse,remoteOffset],events:[{name:'wallet.changed',when:event => event.remoteFlowsChanged === true && event.vendorIds.includes(props.vendorId)}],clearOnChange:true,coalesce:true,eventIntervalMs:5000});
const cursor = ref<string|null>(null), history = ref<(string|null)[]>([]);
const targets = useQuery('wallet.calibration.targets',() => bridge.call('reconciliation.targets',{vendorId:props.vendorId,cursor:cursor.value}).then(unwrap),{visible:historyOpen,deps:[cursor],clearOnChange:true,clearOnHide:true,coalesce:true});
const noticeKey = `htd.calibration.notice.${props.vendorId}`;
const notices = ref(true);
try { notices.value = localStorage.getItem(noticeKey) !== 'off'; } catch { /* Optional preference. */ }
watch(notices, value => {try {if(value)localStorage.removeItem(noticeKey);else localStorage.setItem(noticeKey,'off');}catch{/* Optional preference. */}});
const mismatch = computed(() => batches.data.value?.remoteIntervals.some(row => row.sessionCount > 0 && (BigInt(row.consumptionPico)-BigInt(row.estimatePico) >= 10000000000n || BigInt(row.estimatePico)-BigInt(row.consumptionPico) >= 10000000000n)));
let generation = 0;
function invalidate() {++generation;preview.value=undefined;plan.value=undefined;previewing.value=false;}
watch(draft,invalidate,{deep:true,flush:'sync'});
watch([startText,endText,startValid,endValid],invalidate,{flush:'sync'});
onScopeDispose(invalidate);
function showRange() {startText.value=formatDateTimeInput(new Date(draft.start),props.mode);endText.value=formatDateTimeInput(new Date(draft.end),props.mode);}
watch(() => props.mode,showRange);
function applyRange() {
  try {
    if(!startField.value?.commit() || !endField.value?.commit() || !valid.value)throw new RangeError('请检查起止日期');
    const start=parseDateTimeInput(startText.value,props.mode).toISOString(),end=parseDateTimeInput(endText.value,props.mode).toISOString();
    if(start>=end)throw new RangeError('结束时间须晚于开始时间');
    draft.start=start;draft.end=end;draft.batchIds=[];draft.intervalIds=[];draft.days=[];draft.allocations=[];draft.targetId=undefined;draft.replaceIds=[];draft.confirmScope=false;
    liveBrowse.value=false;browse.value={start,end};remoteOffset.value=0;error.value='';
  }catch(failure){error.value=failure instanceof Error?failure.message:asAppError(failure).message;}
}
function back() {editing.value=false;invalidate();error.value='';startText.value=formatDateTimeInput(new Date(browse.value.start),props.mode);endText.value=formatDateTimeInput(new Date(browse.value.end),props.mode);}
function begin() {draft.start=browse.value.start;draft.end=browse.value.end;draft.intervalIds=[];draft.batchIds=[];draft.targetId=undefined;draft.replaceIds=[];draft.days=[];draft.allocations=[];draft.target=amountDraft();draft.confirmScope=false;editing.value=true;draft.action='apply';draft.basis='manual';draft.allocationMode='proportional';showRange();}
function selectPeriod(row:CommandData<'reconciliation.batches'>['remoteIntervals'][number]) {
  draft.start=row.localStart ?? new Date(Date.parse(row.start)+1).toISOString();draft.end=row.localEnd ?? new Date(Date.parse(row.end)+1).toISOString();
  draft.batchIds=[];draft.intervalIds=[row.id];draft.target=amountDraft(row.consumptionPico);draft.targetId=undefined;draft.replaceIds=[];draft.confirmReplacement=false;draft.confirmScope=false;
  draft.days=[];draft.allocations=[];draft.modelWeights={};draft.allocationMode='proportional';draft.action='apply';
  draft.basis=row.method==='remote_usage'?'remote':'manual';draft.note=row.method==='remote_usage'?'采用所选账户时段消费':'已核实余额推算时段的本地消费归属';editing.value=true;showRange();
}
watch(base.data,value => {
  if(!value)return;
  if(!draft.target.text)draft.target=amountDraft(value.currentEffectivePico);
  if(draft.basis==='remote' && value.remoteTargetPico!==null)draft.target=amountDraft(value.remoteTargetPico);
});
watch(() => props.manual,value => {if(value)begin();});
function editTarget(target:CommandData<'reconciliation.targets'>['targets'][number],revoke=false) {
  draft.start=target.scope.start;draft.end=target.scope.end;draft.batchIds=[...target.scope.localBatchIds];draft.intervalIds=[...target.scope.intervalIds];
  draft.targetId=target.id;draft.replaceIds=[target.id];draft.confirmReplacement=false;draft.action=revoke?'revoke':'apply';draft.basis='manual';draft.target=amountDraft(target.targetPico);
  draft.days=target.dayTargets.map(row=>({day:row.day,amount:amountDraft(row.targetPico)}));
  draft.allocations=target.allocations.map(row=>({day:row.day,model:row.modelKeys[0]??'',harness:row.harnesses[0]??'',group:row.groupIds[0]??'',version:row.versionIds[0]??'',session:row.sessionRefs[0]??'',confirm:row.confirmModelCoverage,amount:amountDraft(row.targetPico),original:row}));
  draft.allocationMode='explicit';editing.value=true;showRange();message.value='';error.value='';
}
function parameters():RequestMap['reconciliation.previewCalibration'] {
  return {...query.value,action:draft.action,targetId:draft.targetId,targetPico:amountPico(draft.target),basis:draft.basis,confirmLocalScope:draft.confirmScope,acknowledgeIncomplete:draft.acknowledgeIncomplete,confirmReplacement:draft.confirmReplacement,replaceTargetIds:[...draft.replaceIds],note:draft.note,
    allocationMode:draft.allocationMode,modelWeights:draft.allocationMode==='weighted'?{...draft.modelWeights}:{},
    dayTargets:draft.allocationMode==='explicit'?draft.days.map(row=>({day:row.day,targetPico:amountPico(row.amount)})):[],
    allocations:draft.allocationMode==='explicit'?draft.allocations.map(row=>({...row.original,day:row.day,targetPico:amountPico(row.amount),modelKeys:row.model?[row.model]:[],harnesses:row.harness?[row.harness as 'codex']:[],groupIds:row.group?[row.group]:[],versionIds:row.version?[row.version]:[],sessionRefs:row.session?[row.session]:[],confirmModelCoverage:row.confirm})):[]};
}
async function showPreview() {
  error.value='';invalidate();
  const current=++generation;
  try {
    if(startText.value!==formatDateTimeInput(new Date(draft.start),props.mode)||endText.value!==formatDateTimeInput(new Date(draft.end),props.mode))throw new RangeError('时间草稿已变化，请先应用时间');
    if(draft.action!=='revoke' && draft.basis==='manual' && !draft.note.trim())throw new RangeError('请填写金额依据');
    const request=parameters();previewing.value=true;
    const result=unwrap(await bridge.call('reconciliation.previewCalibration',request));
    if(current===generation){plan.value=request;preview.value=result;}
  }catch(failure){if(current===generation)error.value=failure instanceof Error?failure.message:asAppError(failure).message;}finally{if(current===generation)previewing.value=false;}
}
async function commit(){if(await session.commit()){historyOpen.value=true;await targets.refresh();await base.refresh();}}
const stateLabels={active:'已锁定',replaced:'已替换',revoked:'已撤销'};
defineExpose({begin});
</script>
<template>
<section class="calibration" aria-label="本地消费校准">
  <header><div><h2>本地消费校准</h2><p>按账户消费时段对齐金额，保留原始估算和校准记录。</p></div><Button v-if="editing" :disabled="pending" @click="back">返回消费时段</Button><Button v-else :pending="batches.refreshing.value" @click="batches.refresh">更新消费时段</Button></header>
  <InlineAlert v-if="error" severity="danger">{{error}} <Button aria-label="关闭校准提醒" @click="error=''">关闭提醒</Button></InlineAlert><p v-if="message" role="status">{{message}}</p>
  <InlineAlert v-if="uncertain" severity="warning">上次操作的结果尚未确认。<Button :pending="pending" @click="commit">查询或重试上次操作</Button></InlineAlert>
  <template v-if="!editing">
    <div class="range"><DateTimeField ref="startField" v-model="startText" label="开始时间" :mode="mode" @validity-change="startValid=$event"/><DateTimeField ref="endField" v-model="endText" label="结束时间" :mode="mode" @validity-change="endValid=$event"/><Button :disabled="!valid" @click="applyRange">应用时间</Button></div>
    <label class="check"><input v-model="notices" type="checkbox"/>显示消费差额提醒</label>
    <InlineAlert v-if="notices && mismatch" severity="warning">部分时段的远程消费与本地估算有差额，请先检查供应商面板、价格和本机是否覆盖全部消费。</InlineAlert>
    <InlineAlert v-if="batches.error.value" severity="danger" retry @retry="batches.refresh">{{batches.error.value.message}}</InlineAlert>
    <p v-if="batches.loading.value">正在读取消费时段…</p>
    <div class="table-wrap"><table aria-label="账户消费时段"><thead><tr><th>本地会话时段</th><th>结算时本地估算</th><th>远程消费</th><th>差额</th><th></th></tr></thead><tbody>
      <tr v-for="row in batches.data.value?.remoteIntervals" :key="row.id"><td>{{formatDateTime(row.localStart??row.start,mode)}}<br/>至 {{formatDateTime(row.localEnd??row.end,mode)}}<small>{{row.sessionCount}} 个会话 · {{row.method==='remote_usage'?'累计消费增量':'余额推算'}}<br/>余额观测：{{formatDateTime(row.start,mode)}} 至 {{formatDateTime(row.end,mode)}}</small></td><td><Money :pico="row.estimatePico"/></td><td><Money :pico="row.consumptionPico"/></td><td><Money :pico="(BigInt(row.consumptionPico)-BigInt(row.estimatePico)).toString()"/></td><td><Button :disabled="readonly || !!uncertain || !row.sessionCount" @click="selectPeriod(row)">校准本地消费</Button></td></tr>
      <tr v-if="!batches.loading.value && !batches.data.value?.remoteIntervals.length"><td colspan="5">当前范围暂无已结算账户消费。账户下全部会话静默十分钟后结算；请确认已配置远程账户，或选择其他时间。</td></tr>
    </tbody></table></div>
    <footer><Button v-if="remoteOffset" @click="remoteOffset=Math.max(0,remoteOffset-100)">上一页时段</Button><Button v-if="batches.data.value?.remoteNextOffset!=null" @click="remoteOffset=batches.data.value!.remoteNextOffset!">下一页时段</Button><Button v-if="!readonly" :disabled="!!uncertain" @click="begin">人工指定目标</Button></footer>
  </template>
  <template v-else>
    <p>{{formatDateTime(draft.start,mode)}} 至 {{formatDateTime(draft.end,mode)}}</p>
    <InlineAlert v-if="base.error.value" severity="danger" retry @retry="base.refresh">{{base.error.value.message}}</InlineAlert>
    <p v-if="base.loading.value">正在读取所选时段金额…</p>
    <dl v-if="base.data.value" class="amounts"><div><dt>本地有效消费</dt><dd><Money :pico="base.data.value.currentEffectivePico"/></dd></div><div><dt>远程消费</dt><dd><Money v-if="base.data.value.remoteTargetPico!==null" :pico="base.data.value.remoteTargetPico"/><span v-else>无远程依据</span></dd></div><div><dt>已有校准</dt><dd><Money :pico="base.data.value.existingCorrectionPico"/></dd></div></dl>
    <form v-if="!readonly && !uncertain" class="surface editor" @submit.prevent="showPreview"><h3>{{draft.action==='revoke'?'撤销校准':'设置校准金额'}}</h3>
      <fieldset :disabled="pending || base.loading.value"><label>本地目标消费（USD）<input v-model="draft.target.text" aria-label="本地目标金额" inputmode="decimal" :readonly="draft.action==='revoke' || draft.basis==='remote'"/></label>
      <template v-if="draft.action!=='revoke'">
        <p v-if="draft.basis==='remote' && base.data.value && !(base.data.value.localComplete && base.data.value.quiet && base.data.value.coverageAligned)">该远程时段尚未确认完整覆盖。<Button type="button" @click="draft.basis='manual'">已核实，改用人工确认</Button></p>
        <label class="check"><input v-model="draft.confirmScope" type="checkbox"/>已核实该时段的远程消费属于本机所选账户，不含其他客户端消费</label>
        <label v-if="!base.data.value?.localComplete || !base.data.value?.quiet" class="check"><input v-model="draft.acknowledgeIncomplete" type="checkbox"/>已核实未静默或覆盖不足时的金额依据</label>
        <label>金额依据与备注<input v-model="draft.note" aria-label="金额依据" maxlength="500"/></label>
        <label>模型成本分配<select v-model="draft.allocationMode" aria-label="模型成本分配"><option value="proportional">按本地消费比例分配差额</option><option value="weighted">自行设置模型比例</option><option value="unallocated">仅校准总额，不分配模型</option><option v-if="draft.allocations.length || draft.days.length" value="explicit">保留原明确分配</option></select></label>
        <p class="text-muted">先检查供应商管理面板是否有具体模型消费差异，再考虑配平。比例分配是人工估计，会影响模型成本与性价比；按日保存，小时模型成本保留原估算。</p>
        <div v-if="draft.allocationMode==='weighted'" class="weights"><label v-for="model in base.data.value?.modelKeys" :key="model">{{model||'未知模型'}}<input v-model="draft.modelWeights[model]" :aria-label="`${model}分配比例`" inputmode="decimal" placeholder="0 到 1"/></label><p>比例合计为1；0表示不分配，1表示全部差额分配给该模型。</p></div>
        <label v-if="draft.replaceIds.length" class="check"><input v-model="draft.confirmReplacement" type="checkbox"/>确认完整替换原校准目标</label>
      </template><p v-else>撤销后按当前估算恢复本地消费与余额。</p>
      </fieldset><Button type="submit" :pending="previewing" :disabled="pending || base.loading.value">预览金额影响</Button>
    </form>
    <section v-if="preview" class="surface plan" aria-label="校准预览"><h3>确认金额影响</h3><dl class="amounts"><div><dt>目标消费</dt><dd><Money :pico="preview.targetPico"/></dd></div><div><dt>本次消费差额</dt><dd><Money :pico="preview.deltaPico"/></dd></div><div><dt>本地余额变化</dt><dd><Money :pico="(-BigInt(preview.deltaPico)).toString()"/></dd></div></dl><p>同时更新本地累计消费、消费总额及适用日期的有效成本，充值金额不变。</p>
      <p v-for="day in preview.days" :key="day.day">{{day.day}}：目标 <Money :pico="day.targetPico"/>，未明确分配 <Money :pico="day.unallocatedPico"/></p>
      <div v-if="preview.modelTargets.length" class="table-wrap"><table aria-label="模型校准分配"><thead><tr><th>日期 / 模型</th><th>原估算</th><th>差额</th><th>校准后</th></tr></thead><tbody><tr v-for="row in preview.modelTargets" :key="`${row.day}/${row.modelKey}`"><td>{{row.day}} / {{row.modelKey}}</td><td><Money :pico="row.estimatePico"/></td><td><Money :pico="row.deltaPico"/></td><td><Money :pico="row.targetPico"/></td></tr></tbody></table></div>
      <Button variant="primary" :pending="pending" @click="commit">{{preview.action==='revoke'?'确认撤销':'确认采用目标'}}</Button>
    </section>
  </template>
  <Button :aria-expanded="historyOpen" @click="historyOpen=!historyOpen">{{historyOpen?'收起校准记录':'查看校准记录'}}</Button>
  <section v-if="historyOpen" class="history"><h3>校准记录</h3><InlineAlert v-if="targets.error.value" severity="danger">{{targets.error.value.message}}</InlineAlert><article v-for="target in targets.data.value?.targets" :key="target.id" class="surface"><header><strong>{{stateLabels[target.state]}} · <Money :pico="target.targetPico"/></strong><span>{{formatDateTime(target.createdAt,mode)}}</span></header><p>{{formatDateTime(target.scope.start,mode)}} 至 {{formatDateTime(target.scope.end,mode)}} · 含校准金额 <Money :pico="target.deltaPico"/></p><footer v-if="target.state==='active' && !readonly"><Button :disabled="pending || !!uncertain" @click="editTarget(target)">更正目标</Button><Button :disabled="pending || !!uncertain" @click="editTarget(target,true)">撤销目标</Button></footer></article><p v-if="!targets.loading.value && !targets.data.value?.targets.length">尚无校准记录。</p><footer><Button v-if="history.length" @click="cursor=history.pop()??null">上一页记录</Button><Button v-if="targets.data.value?.nextCursor" @click="history.push(cursor);cursor=targets.data.value!.nextCursor">下一页记录</Button></footer></section>
</section>
</template>
<style scoped>
.calibration,.editor,.history,.plan{display:grid;gap:18px;min-width:0}header,footer,.range{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}h2{font-size:19px}h3{font-size:16px}p,small,label{font-size:13px;line-height:1.7}small{display:block;color:var(--text-muted);margin-top:6px}.surface{padding:20px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:14px 12px;border-bottom:1px solid var(--border)}th{color:var(--text-muted);font-weight:500}td:not(:first-child){white-space:nowrap}.amounts{display:flex;gap:32px;flex-wrap:wrap;padding:16px 0;border-block:1px solid var(--border);margin:0}dt{font-size:12px;color:var(--text-muted)}dd{font-size:22px;margin:6px 0 0}.editor fieldset{border:0;padding:0;display:grid;gap:16px;min-width:0}label{display:grid;gap:6px}.check{display:flex;align-items:center;gap:8px}.check input{min-height:0}input:not([type=checkbox]),select{padding:9px;max-width:100%;width:min(100%,420px)}.weights{display:flex;gap:18px;flex-wrap:wrap}.weights p{width:100%}.plan{border-color:var(--accent)}.editor>button,.plan>button,.calibration>button{justify-self:start}
</style>
