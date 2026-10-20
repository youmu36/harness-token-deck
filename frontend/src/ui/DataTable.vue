<script setup lang="ts" generic="T extends Record<string, unknown>">
import { computed, ref, watch } from 'vue';
import type { TableColumn } from './table';

const props = withDefaults(defineProps<{
  rows: readonly T[]; columns: readonly TableColumn<T>[]; rowKey?: string | ((row: T, index: number) => string | number);
  height?: number; rowHeight?: number; label?: string; resetKey?: string; initialSort?: { key: string; direction: 'asc' | 'desc' };
}>(), { rowKey: 'id', height: 440, rowHeight: 40, label: '数据表格' });
const emit = defineEmits<{ sort: [key: string, direction: 'asc' | 'desc'] }>();
const sortKey = ref<string | undefined>(props.initialSort?.key);
const sortDirection = ref<'asc' | 'desc'>(props.initialSort?.direction ?? 'asc');
const scrollTop = ref(0);
const scroller = ref<HTMLElement>();
const virtual = computed(() => props.rows.length > 200);
const sortedRows = computed(() => {
  const rows = [...props.rows];
  const column = props.columns.find(item => item.key === sortKey.value);
  if (!column) return rows;
  return rows.sort((a, b) => {
    const av = a[column.key]; const bv = b[column.key];
    const result = column.compare ? column.compare(a, b)
      : av === bv ? 0 : av === undefined || av === null ? 1 : bv === undefined || bv === null ? -1
      : typeof av === 'number' && typeof bv === 'number' ? av - bv
      : typeof av === 'bigint' && typeof bv === 'bigint' ? (av > bv ? 1 : -1)
      : String(av).localeCompare(String(bv), 'zh-CN', { numeric: true });
    return sortDirection.value === 'asc' ? result : -result;
  });
});
const start = computed(() => virtual.value ? Math.min(Math.max(0, sortedRows.value.length - 1), Math.max(0, Math.floor(scrollTop.value / props.rowHeight) - 3)) : 0);
const end = computed(() => virtual.value ? Math.min(sortedRows.value.length, start.value + Math.ceil(props.height / props.rowHeight) + 7) : sortedRows.value.length);
const visibleRows = computed(() => sortedRows.value.slice(start.value, end.value));
function resetScroll() { scrollTop.value = 0; if (scroller.value) scroller.value.scrollTop = 0; }
// Background snapshots replace row arrays; only a deliberate filter change resets position.
watch(() => props.resetKey, resetScroll);
function toggleSort(key: string) {
  if (sortKey.value === key) sortDirection.value = sortDirection.value === 'asc' ? 'desc' : 'asc';
  else { sortKey.value = key; sortDirection.value = 'asc'; }
  resetScroll(); emit('sort', key, sortDirection.value);
}
function keyFor(row: T, index: number) {
  return typeof props.rowKey === 'function' ? props.rowKey(row, index) : String(row[props.rowKey] ?? index);
}
</script>

<template>
  <div ref="scroller" class="table-wrap" :style="{ maxHeight: `${height}px` }" tabindex="0" :aria-label="label"
    @scroll="scrollTop = ($event.target as HTMLElement).scrollTop">
    <table :aria-label="label" :aria-rowcount="rows.length + 1">
      <thead><tr><th v-for="column in columns" :key="column.key" scope="col" :class="{ numeric: column.numeric }"
        :aria-sort="column.sortable ? (sortKey === column.key ? (sortDirection === 'asc' ? 'ascending' : 'descending') : 'none') : undefined">
        <button v-if="column.sortable" type="button" @click="toggleSort(column.key)">
          {{ column.label }} <span aria-hidden="true">{{ sortKey === column.key ? (sortDirection === 'asc' ? '↑' : '↓') : '↕' }}</span>
        </button><template v-else>{{ column.label }}</template>
      </th></tr></thead>
      <tbody>
        <tr v-if="!sortedRows.length"><td :colspan="columns.length"><slot name="empty"><span class="text-muted">暂无数据</span></slot></td></tr>
        <tr v-if="virtual && start" aria-hidden="true" class="spacer"><td :colspan="columns.length" :style="{ height: `${start * rowHeight}px` }" /></tr>
        <tr v-for="(row, index) in visibleRows" :key="keyFor(row, start + index)" :aria-rowindex="start + index + 2"
          :style="virtual ? { height: `${rowHeight}px` } : undefined">
          <td v-for="column in columns" :key="column.key" :class="{ numeric: column.numeric }">
            <slot :name="`cell-${column.key}`" :row="row" :value="row[column.key]">
              {{ column.format ? column.format(row[column.key], row) : (row[column.key] ?? '--') }}
            </slot>
          </td>
        </tr>
        <tr v-if="virtual && end < rows.length" aria-hidden="true" class="spacer"><td :colspan="columns.length" :style="{ height: `${(rows.length - end) * rowHeight}px` }" /></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-wrap { overflow: auto; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
table { width: 100%; border-collapse: separate; border-spacing: 0; }
th, td { padding: 8px 12px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }
th { position: sticky; top: 0; z-index: 1; background: var(--surface-2); font-weight: 600; }
th button { border: 0; background: transparent; padding: 0; font-weight: inherit; }
.numeric { text-align: right; }
td { max-width: 480px; overflow: hidden; text-overflow: ellipsis; }
.spacer td { padding: 0; border: 0; }
</style>
