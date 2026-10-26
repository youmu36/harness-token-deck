<script setup lang="ts">
import { computed, inject, ref } from 'vue';
import { X } from 'lucide-vue-next';
import { BridgeKey } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { harnessNames, type Harness } from '../harness/types';
import InlineAlert from '../../ui/InlineAlert.vue';
import IconButton from '../../ui/IconButton.vue';
const props = defineProps<{ harness?: Harness }>();
const bridge = inject(BridgeKey)!;
const query = useQuery('reconciliation.status', () => bridge.call('reconciliation.status', {}).then(unwrap), { events: ['wallet.changed', { name: 'pricing.changed', immediate: true }, 'usage.changed'], coalesce: true, eventIntervalMs: 60_000 });
const notices = computed(() => query.data.value?.notices.filter(n => !props.harness || n.harness === props.harness) ?? []);
const dismissing = ref(false);
const dismissError = ref('');
async function dismiss(notice: typeof notices.value[number]) {
  dismissing.value = true; dismissError.value = '';
  try { unwrap(await bridge.call('reconciliation.dismiss', { vendorId: notice.vendorId, observedAt: notice.observedAt })); await query.refresh(); }
  catch (error) { dismissError.value = asAppError(error).message; }
  finally { dismissing.value = false; }
}
</script>
<template>
  <div v-if="notices.length || query.error.value || dismissError" class="reconciliation-notices" aria-label="供应商校准提醒">
    <InlineAlert v-if="query.error.value" severity="warning" retry @retry="query.refresh">暂时无法读取校准提醒：{{ query.error.value.message }}</InlineAlert>
    <InlineAlert v-if="dismissError" severity="warning">关闭提醒失败：{{ dismissError }}</InlineAlert>
    <InlineAlert v-for="notice in notices" :key="`${notice.vendorId}/${notice.harness}`" severity="warning">
      <div class="notice-content"><p>{{ notice.vendorName }} 的远程账户出现扣费，{{ harnessNames[notice.harness] }} 当前价格组“{{ notice.selectedGroupName || '未选择' }}”关联的供应商与之不同。请核对实际使用的供应商，并切换到对应价格组。<a :href="`#/harness/${notice.harness}`">前往 {{ harnessNames[notice.harness] }} 切换价格组</a></p><IconButton :label="`关闭 ${notice.vendorName} 的供应商校准提醒`" :disabled="dismissing" @click="dismiss(notice)"><X :size="16" aria-hidden="true" /></IconButton></div>
    </InlineAlert>
  </div>
</template>
<style scoped>.reconciliation-notices { display: grid; gap: 10px; }.reconciliation-notices :deep(.alert > div){width:100%}.notice-content{display:flex;align-items:start;gap:12px}.notice-content p{flex:1;min-width:0}.notice-content :deep(button){flex-shrink:0}a { margin-left: 8px; }</style>
