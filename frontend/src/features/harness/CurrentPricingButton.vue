<script setup lang="ts">
import { computed, ref } from 'vue';
import { versionName, type Catalog, type Harness, type PricingSelection } from './types';
import SwitchPricingDialog from './SwitchPricingDialog.vue';
import { SlidersHorizontal } from 'lucide-vue-next';
const props = defineProps<{ harness: Harness; catalog?: Catalog; loading?: boolean; error?: string }>();
const emit = defineEmits<{ switched: [value: PricingSelection]; retry: [] }>();
const open = ref(false);
const group = computed(() => props.catalog?.groups.find(group => group.id === props.catalog?.current[props.harness]?.groupId));
const version = computed(() => group.value?.versions.find(version => version.id === props.catalog?.current[props.harness]?.versionId));
</script>

<template>
  <div class="current-pricing">
    <button type="button" class="pricing-button" :disabled="!catalog || loading" :title="error" @click="open = true"><span class="pricing-label">当前价格组与版本</span><span class="pricing-name">{{ loading || !catalog ? '---' : group ? `${group.name} · ${version ? versionName(version) : '未选择版本'}` : '未选择价格组' }}</span><span class="switch-label"><SlidersHorizontal :size="15" aria-hidden="true" />切换</span></button>
    <button v-if="error" type="button" class="retry" @click="emit('retry')">读取失败，重试</button>
    <SwitchPricingDialog v-if="catalog && open" v-model:open="open" :harness="harness" :catalog="catalog" @switched="emit('switched', $event)" />
  </div>
</template>

<style scoped>
.current-pricing { min-width: 0; }.pricing-button { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; text-align: left; font-size: 12px; }
.pricing-label { color: var(--text-muted); }.pricing-name { font-weight: 600; overflow-wrap: anywhere; }.switch-label { display: inline-flex; align-items: center; gap: 6px; color: var(--accent); }
.pricing-button:disabled { color: var(--text-muted); }.pricing-button:hover:not(:disabled) { border-color: var(--accent); }.retry { display: block; background: transparent; border: 0; font-size: 12px; color: var(--danger); }
</style>
