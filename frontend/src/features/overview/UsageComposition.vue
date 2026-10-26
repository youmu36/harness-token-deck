<script setup lang="ts">
import { computed } from 'vue';
import type { CommandData } from '../../app/ports';
import Money from '../../ui/Money.vue';
const props = defineProps<{ snapshot: CommandData<'stats.explore'>; theme?:'cost'|'cache'; scopeLabel?:string }>();
const colors = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)'];
const parts = computed(() => {
  const keys = [{ key: 'input', name: '未命中输入' }, { key: 'cache', name: '缓存命中' }, { key: 'output', name: '输出' }, { key: 'write', name: '计费缓存创建' }];
  const total = BigInt(props.theme === 'cache' ? props.snapshot.totals.totalTokens : props.snapshot.totals.eligibleTokens);
  let offset = 0;
  return keys.map((part, index) => {
    const actual:Record<string,string> = {input:props.snapshot.totals.newInput,cache:props.snapshot.totals.cacheReadTokens,output:props.snapshot.totals.outputTokens,write:'0'};
    const count = BigInt((props.theme === 'cache' ? actual[part.key] : props.snapshot.totals.parts[part.key]) ?? '0');
    const share = total > 0n && count > 0n ? Number(count * 100000000n / total) / 1000000 : 0;
    const result = { ...part, nonzero:count > 0n, count: count.toLocaleString('en-US'), share, offset, color: colors[index] };
    offset += share;
    return result;
  });
});
const ranking = computed(() => {
  const total = BigInt(props.snapshot.totals.picoUsd) - BigInt(props.snapshot.totals.unallocatedCorrectionPico);
  const rows = props.snapshot.models.slice(0, 5).map(row => ({ key: row.modelKey, name: row.displayName, cost: row.picoUsd }));
  const shown = rows.reduce((sum, row) => sum + BigInt(row.cost), 0n);
  if (total > shown) rows.push({ key: '__other__', name: '其他模型', cost: String(total - shown) });
  return rows.map((row, index) => ({ ...row, color: colors[index % colors.length], share: total > 0n ? Number(BigInt(row.cost) * 100000000n / total) / 1000000 : 0 }));
});
</script>

<template>
  <section :class="['composition','surface',{single:theme}]" aria-label="用量构成与模型费用分布">
    <div v-if="theme !== 'cost'" class="token-composition"><h3>Token 使用结构<span v-if="scopeLabel"> · {{scopeLabel}}</span></h3><div class="composition-content">
      <svg class="donut" viewBox="0 0 120 120" role="img" aria-label="Token 构成环形图"><circle cx="60" cy="60" r="44" fill="none" stroke="var(--surface-2)" stroke-width="16" /><circle v-for="part in parts.filter(row => row.share > 0)" :key="part.key" cx="60" cy="60" r="44" fill="none" :stroke="part.color" stroke-width="16" pathLength="100" :stroke-dasharray="`${part.share} ${100 - part.share}`" :stroke-dashoffset="-part.offset" transform="rotate(-90 60 60)"><title>{{ part.name }}：{{ part.count }} Token，{{ part.share.toFixed(2) }}%</title></circle><text x="60" y="57" text-anchor="middle">{{ BigInt(theme === 'cache' ? snapshot.totals.totalTokens : snapshot.totals.eligibleTokens) > 0n ? '100%' : '—' }}</text><text x="60" y="74" text-anchor="middle" class="donut-label">{{ theme === 'cache' ? '所选用量' : '完整样本' }}</text></svg>
      <ul><li v-for="part in parts" :key="part.key"><span class="swatch" :style="{background:part.color}"></span><span>{{ part.name }}<small>{{ part.count }} Token</small></span><strong>{{ part.share.toFixed(2) }}%{{ part.nonzero && part.share < .005 ? '（非零）' : '' }}</strong></li></ul>
    </div><p>{{ theme === 'cache' ? '按所选用量展示' : '按已定价且字段完整样本展示' }}，覆盖 {{ BigInt(theme === 'cache' ? snapshot.totals.totalTokens : snapshot.totals.eligibleTokens).toLocaleString('en-US') }} Token；缺失信息不当作零。</p></div>
    <div v-if="theme !== 'cache'" class="cost-ranking"><h3>模型费用分布<span v-if="scopeLabel"> · {{scopeLabel}}</span></h3><ul><li v-for="row in ranking" :key="row.key"><div><span>{{ row.name }}</span><Money :pico="row.cost" /></div><div class="rank-track"><span :style="{width:`${row.share}%`,background:row.color}"></span></div><small>{{ row.share.toFixed(2) }}%{{ BigInt(row.cost) > 0n && row.share < .005 ? '（非零）' : '' }}</small></li></ul><p v-if="snapshot.totals.unallocatedCorrectionPico !== '0'">未分配校准 <Money :pico="snapshot.totals.unallocatedCorrectionPico" />，独立列示。</p><p v-if="!ranking.length">当前条件暂无可显示的费用。</p></div>
  </section>
</template>

<style scoped>
.composition{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:32px;padding:22px}h3{font-size:15px;margin-bottom:16px}.composition.single{grid-template-columns:1fr}.composition-content{display:flex;align-items:center;gap:20px}.donut{width:150px;flex:0 0 150px}.donut text{font-size:17px;fill:var(--text);font-weight:600}.donut .donut-label{font-size:10px;fill:var(--text-muted);font-weight:400}ul{list-style:none;padding:0;margin:0;display:grid;gap:12px;width:100%}.token-composition li{display:flex;align-items:center;gap:9px;font-size:12px}.swatch{width:9px;height:9px;border-radius:2px;flex-shrink:0}li strong{margin-left:auto;font-weight:500}small{display:block;color:var(--text-muted);font-size:11px;margin-top:4px}.cost-ranking li{display:grid;grid-template-columns:minmax(0,1fr) 42px;gap:4px 10px;font-size:12px}.cost-ranking li>div:first-child{display:flex;justify-content:space-between;gap:10px;grid-column:1/-1}.rank-track{height:7px;background:var(--surface-2);align-self:center;border-radius:2px;overflow:hidden}.rank-track span{display:block;height:100%}.cost-ranking small{margin:0;text-align:right}p{font-size:11px;color:var(--text-muted);line-height:1.8;margin-top:16px}@media(max-width:1050px){.composition{grid-template-columns:1fr}}@media(max-width:600px){.composition.single{grid-template-columns:1fr}.composition-content{flex-wrap:wrap}.donut{margin:auto}}
</style>
<style scoped>
.composition{padding:16px;gap:20px}.composition.single{max-width:760px}.composition-content{gap:16px}.donut{width:124px;flex-basis:124px}h3{margin-bottom:12px}ul{gap:9px}p{margin-top:10px;font-size:12px}
</style>
