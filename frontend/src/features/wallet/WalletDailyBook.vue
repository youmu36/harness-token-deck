<script setup lang="ts">
import { inject, ref } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import Money from '../../ui/Money.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const props=defineProps<{vendorId:string;scope:'local'|'remote'}>();
const bridge=inject(BridgeKey)!;
const now=new Date();
const month=ref(`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`);
const query=useQuery('wallet.dailyBook',async()=>unwrap(await bridge.call('wallet.dailyBook',{vendorId:props.vendorId,scope:props.scope,month:month.value})),{deps:[()=>props.vendorId,()=>props.scope,month],clearOnChange:true,coalesce:true,eventIntervalMs:3*3600_000,events:[{name:'wallet.changed',when:e=>e.vendorIds.includes(props.vendorId)}]});
</script>
<template>
  <section class="daily-book surface" aria-label="钱包记账本">
    <header><h2>{{scope==='local'?'本地':'远程'}}记账本</h2><label>月份<input v-model="month" type="month" aria-label="记账月份" /></label><Button :pending="query.loading.value||query.refreshing.value" @click="query.refresh">更新记账</Button></header>
    <InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{query.error.value.message}}</InlineAlert>
    <template v-if="query.data.value">
      <div class="month-totals"><p>本月充值 <Money :pico="query.data.value.R" /></p><p>本月消费 <Money :pico="query.data.value.C" /></p></div>
      <p class="text-muted">{{query.data.value.notice}}</p><p v-if="query.data.value.providerObservedAt" class="text-muted">供应商返回的当前 API Key 参考：今日 <Money v-if="query.data.value.providerTodayPico !== null" :pico="query.data.value.providerTodayPico" />，累计 <Money v-if="query.data.value.providerTotalPico !== null" :pico="query.data.value.providerTotalPico" />。仅此 Key 的统计，不并入账户账单；观测于 {{query.data.value.providerObservedAt}}。</p><p v-if="query.data.value.pending" role="status">部分日期正在结算或回填，请稍后更新。</p>
      <div class="table-wrap"><table><thead><tr><th>业务日</th><th>充值</th><th>消费</th></tr></thead><tbody><tr v-for="day in query.data.value.days" :key="day.day"><td>{{day.day}}</td><td><Money :pico="day.R" /></td><td><Money :pico="day.C" /></td></tr><tr v-if="!query.data.value.days.length"><td colspan="3">此月暂无已结账目</td></tr></tbody></table></div>
    </template><p v-else-if="query.loading.value">正在读取记账本…</p>
  </section>
</template>
<style scoped>.daily-book{display:grid;gap:18px;padding:20px}header,.month-totals{display:flex;align-items:center;flex-wrap:wrap;gap:16px}header h2{margin-right:auto}label{display:flex;align-items:center;gap:8px}.month-totals{justify-content:space-between;padding-block:12px;border-block:1px solid var(--border)}p{font-size:13px}.table-wrap{overflow:auto;max-height:480px}table{width:100%;border-collapse:collapse}th,td{padding:12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border)}th{position:sticky;top:0;background:var(--surface-2)}</style>
