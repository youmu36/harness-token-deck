<script setup lang="ts">
import type { Vendor, WalletAction } from './types';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
defineProps<{ vendor: Vendor; scope?: 'local'|'remote' }>();
defineEmits<{ action: [value: WalletAction]; correct: [target: 'top_up' | 'consumption']; local: [] }>();
</script>
<template>
  <section class="vendor-settings surface">
    <h2>供应商设置</h2>
    <template v-if="vendor.status === 'active'">
      <div><p><strong>名称与网址</strong><span>{{ vendor.url || '尚未填写供应商网址' }}</span></p><Button @click="$emit('action', 'rename')">编辑供应商</Button></div>
      <div><p><strong>过往充值</strong><span><Money :pico="scope === 'local' ? (vendor.localWallet?.pastTopUpPico ?? vendor.pastTopUpPico ?? '0') : (vendor.pastTopUpPico ?? '0')" mode="usd4" /> · 独立记录，不影响余额与流水。</span></p><Button @click="scope === 'local' ? $emit('local') : $emit('action', 'rename')">修改过往充值</Button></div>
      <div><p><strong>过往消费</strong><span><Money :pico="scope === 'local' ? (vendor.localWallet?.pastConsumptionPico ?? vendor.pastConsumptionPico ?? '0') : (vendor.pastConsumptionPico ?? '0')" mode="usd4" /> · 独立记录，不影响余额与流水。</span></p><Button @click="scope === 'local' ? $emit('local') : $emit('action', 'rename')">修改过往消费</Button></div>
      <div><p><strong>关联价格组</strong><span>关联只影响之后入账的请求，已计入的消费不会转移。</span></p><Button @click="$emit('action', 'link')">关联价格组</Button></div>
      <div><p><strong>累计金额人工校准</strong><span>充值请在资金流水中逐笔校准；消费可逐笔修改或校准累计金额。</span></p><Button @click="$emit('correct', 'top_up')">查看充值与校准</Button><Button @click="$emit('correct', 'consumption')">校准累计消费</Button></div>
      <div class="archive-action"><p><strong>删除或归档</strong><span>有资金记录时保留历史并解除价格组关联。</span></p><Button @click="$emit('action', 'delete')">删除 / 归档</Button></div>
    </template>
    <div v-else><p><strong>恢复供应商</strong><span>历史记录保留，恢复后可重新关联价格组。</span></p><Button @click="$emit('action', 'restore')">恢复供应商</Button></div>
  </section>
</template>
<style scoped>
.vendor-settings { padding: 24px; }.vendor-settings > div { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; padding-block: 22px; border-bottom: 1px solid var(--border); }.vendor-settings > div:last-child { border-bottom: 0; padding-bottom: 0; }
p { display: grid; gap: 6px; }p span { color: var(--text-muted); font-size: 13px; }strong { font-weight: 600; }.archive-action strong { color: var(--danger); }
</style>
