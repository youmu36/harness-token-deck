<script setup lang="ts">
import Button from '../../ui/Button.vue';
defineProps<{ models: readonly string[]; pending: boolean; disabled: boolean }>();
defineEmits<{ restore: [modelId: string] }>();
</script>
<template><details class="excluded"><summary>已移出（{{ models.length }}）</summary><p class="text-muted">自动同步会跳过这些模型；恢复后基准价留空。</p><ul><li v-for="model in models" :key="model"><span>{{ model }}</span><Button :disabled="pending || disabled" @click="$emit('restore', model)">恢复</Button></li></ul><p v-if="!models.length">暂无已移出的模型</p></details></template>
<style scoped>.excluded { border-top: 1px solid var(--border); padding-top: 16px; }summary { cursor: pointer; }p { margin-block: 12px; }ul { list-style: none; padding: 0; }li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; }li span { overflow-wrap: anywhere; }</style>
