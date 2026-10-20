<script setup lang="ts">
import { computed, inject } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { harnessNames, type Harness } from '../harness/types';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ harness?: Harness }>();
const bridge = inject(BridgeKey)!;
const query = useQuery('reconciliation.status', () => bridge.call('reconciliation.status', {}).then(unwrap), { events: ['wallet.changed', { name: 'pricing.changed', immediate: true }, 'usage.changed'], coalesce: true, eventIntervalMs: 3 * 3600_000 });
const notices = computed(() => query.data.value?.notices.filter(n => !props.harness || n.harness === props.harness) ?? []);
</script>
<template>
  <div v-if="notices.length || query.error.value" class="reconciliation-notices" aria-label="供应商校准提醒">
    <InlineAlert v-if="query.error.value" severity="warning" retry @retry="query.refresh">暂时无法读取校准提醒：{{ query.error.value.message }}</InlineAlert>
    <InlineAlert v-for="notice in notices" :key="`${notice.vendorId}/${notice.harness}`" severity="warning">
      {{ notice.vendorName }} 的远程账户出现扣费，{{ harnessNames[notice.harness] }} 当前价格组“{{ notice.selectedGroupName || '未选择' }}”关联的供应商与之不同。请核对实际使用的供应商，并切换到对应价格组。<a :href="`#/harness/${notice.harness}`">前往 {{ harnessNames[notice.harness] }} 切换价格组</a>
    </InlineAlert>
  </div>
</template>
<style scoped>.reconciliation-notices { display: grid; gap: 10px; }a { margin-left: 8px; }</style>
