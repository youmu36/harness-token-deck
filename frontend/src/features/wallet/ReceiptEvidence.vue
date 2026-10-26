<script setup lang="ts">
import { computed, inject, ref } from 'vue';
import { BridgeKey, type RequestMap } from '../../app/ports';
import { useQuery } from '../../shared/query';
import { readShared } from '../../shared/readService';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';

const props = defineProps<{ scope: RequestMap['stats.evidence'] }>();
const bridge = inject(BridgeKey)!;
const open = ref(false), offset = ref(0);
const parameters = computed(() => ({ ...props.scope, offset: offset.value }));
const query = useQuery('wallet.evidence.chain', signal => readShared(bridge, 'stats.evidence', parameters.value, signal), {
  deps: [() => JSON.stringify(parameters.value)], visible: open, clearOnChange: true, coalesce: true,
  events: ['usage.changed', 'cost.changed', 'sources.changed'],
});
</script>

<template>
  <details class="receipt-evidence surface" @toggle="open = ($event.target as HTMLDetailsElement).open">
    <summary>按需诊断：来源、处理进度与消费凭证</summary>
    <template v-if="open"><p>仅显示数值和引用。估值退出普通 72 小时重算窗口后，原凭证、批次与目标仍可核实。</p>
      <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert>
      <p v-if="query.loading.value">正在读取当前页证据…</p>
      <template v-if="query.data.value"><p>来源状态：{{ Object.entries(query.data.value.sourceStates).map(([name,state]) => `${name}: ${state}`).join('、') || '尚无读取证据' }}</p>
        <div class="table-wrap"><table aria-label="请求凭证诊断"><thead><tr><th>发生 / 接收</th><th>处理修订</th><th>价格与凭证</th><th>可用能力</th></tr></thead><tbody><tr v-for="item in query.data.value.items" :key="`${item.harness}:${item.requestKey}`"><td>{{ item.harness }} · {{ item.occurredAt }}<br />{{ item.receivedAt }}<details><summary>身份与读取水位</summary><p>会话 {{ item.sessionRef ?? '未归属' }}<br />请求 {{ item.requestKey }}<br />{{ item.watermark }}</p></details></td><td>接收 {{ item.acceptedRevision }}<br />估值 {{ item.valuedRevision }}<br />投影 {{ item.projectedRevision }}<br />发布 {{ item.publishedRevision }}</td><td>价格组 {{ item.groupId ?? item.originalGroupId ?? '未知' }}<br />版本 {{ item.versionId ?? item.originalVersionId ?? '未知' }}<br />价格修订 {{ item.priceRevisionId ?? '逐请求依据已退休' }}<br />凭证序号 {{ item.voucherSequence ?? '尚未生成' }}<br />核对批次 {{ item.localBatchId ?? '尚未生成' }}<br />账户区间 {{ item.remoteIntervalIds.length }}<br />锁定目标 {{ item.calibrationTargetId ?? '无' }}</td><td>{{ item.state }}<br />{{ item.canRecalculate ? '可普通重算' : '当前无普通重算能力' }}</td></tr><tr v-if="!query.data.value.items.length"><td colspan="4">当前条件没有可用请求证据。</td></tr></tbody></table></div>
        <p>查询修订：{{ Object.entries(query.data.value.snapshot).map(([name,revision]) => `${name}=${revision}`).join('，') }}</p><footer><Button v-if="offset" @click="offset = Math.max(0, offset - 50)">上一页证据</Button><Button v-if="query.data.value.nextOffset !== null" @click="offset = query.data.value!.nextOffset!">下一页证据</Button></footer>
      </template></template>
  </details>
</template>

<style scoped>
.receipt-evidence{padding:16px;min-width:0}summary{cursor:pointer;font-size:13px}p{font-size:12px;line-height:1.7;margin:12px 0;overflow-wrap:anywhere}.table-wrap{overflow:auto;max-height:460px}table{font-size:12px;width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--border);vertical-align:top;min-width:150px;max-width:360px;overflow-wrap:anywhere}th{position:sticky;top:0;background:var(--surface-2)}footer{display:flex;gap:12px;justify-content:flex-end;padding-top:12px}
</style>
