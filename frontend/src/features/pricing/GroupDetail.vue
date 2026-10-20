<script setup lang="ts">
import type { Group } from './types';
import Button from '../../ui/Button.vue';
defineProps<{ group: Group; vendorName?: string }>();
defineEmits<{ action: [action: 'rename' | 'copy' | 'disable' | 'delete'] }>();
</script>
<template>
  <section class="group-detail surface">
    <h2>价格组设置</h2>
    <p v-if="!group.historical" class="vendor-link">关联供应商：<a v-if="group.vendorId" :href="`#/wallet/${encodeURIComponent(group.vendorId)}`">{{ vendorName ?? group.vendorId }}</a><span v-else>未关联</span> <a href="#/wallet">管理关联</a></p>
    <div class="setting-row"><p><strong>分组名称</strong><span>{{ group.name }}</span></p><Button :disabled="group.disabled || group.historical" @click="$emit('action', 'rename')">重命名</Button></div>
    <div class="setting-row"><p><strong>复制价格方案</strong><span>保留原分组，建立一个独立价格组。</span></p><Button @click="$emit('action', 'copy')">复制为新组</Button></div>
    <div class="setting-row"><p><strong>{{ group.disabled ? '启用分组' : '停用分组' }}</strong><span>{{ !group.disabled && group.harnesses.length ? '请先切换正在使用此分组的 Harness。' : '停用后保留历史价格，仅停止新的选择与编辑。' }}</span></p><span :title="group.harnesses.length ? '请先切换正在使用此分组的 Harness' : undefined"><Button :disabled="group.historical || (!group.disabled && !!group.harnesses.length)" @click="$emit('action', 'disable')">{{ group.disabled ? '启用' : '停用' }}</Button></span></div>
    <div class="setting-row"><p><strong class="danger">删除分组</strong><span>{{ !group.canDelete ? '价格组已删除。' : '删除后不可恢复，历史价格和已计费用量保留。' }}</span></p><span :title="!group.canDelete ? '价格组已删除' : undefined"><Button :disabled="group.historical || !group.canDelete" @click="$emit('action', 'delete')">删除</Button></span></div>
  </section>
</template>
<style scoped>
.group-detail { padding: 24px; }.vendor-link { font-size: 13px; color: var(--text-muted); margin-block: 12px 4px; }.vendor-link > a:last-child { margin-left: 8px; }
.setting-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px; padding-block: 22px; border-bottom: 1px solid var(--border); }.setting-row:last-child { border-bottom: 0; padding-bottom: 0; }.setting-row p { display: grid; gap: 6px; min-width: 0; }.setting-row p span { color: var(--text-muted); font-size: 13px; overflow-wrap: anywhere; }.setting-row strong { font-weight: 600; }.danger { color: var(--danger); }
</style>
