<script setup lang="ts">
import { inject, ref, shallowRef, watch } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { unwrap, asAppError } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { formatDateTime } from '../../shared/format';
import type { TimeZoneMode } from '../../shared/time';
import type { Vendor } from './types';
import Money from '../../ui/Money.vue';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import { entryNote, amountExplanation, noteExplanation } from './entryNotes';
import HelpHint from '../../ui/HelpHint.vue';
const props = defineProps<{ vendor: Vendor; mode: TimeZoneMode }>();
const bridge = inject(BridgeKey)!;
const kind = ref<'all' | 'top_up' | 'adjustment' | 'consumption_daily'>('all');
const offset = () => props.mode === 'UTC' ? 0 : -new Date().getTimezoneOffset();
const loadedPages = ref(1);
const first = useQuery('wallet.entries', async () => {
  let result = unwrap(await bridge.call('wallet.entries', { vendorId: props.vendor.id, limit: 50, kind: kind.value, offsetMinutes: offset() }));
  const entries = [...result.entries];
  for (let page = 1; page < loadedPages.value && result.nextCursor; page++) {
    result = unwrap(await bridge.call('wallet.entries', { vendorId: props.vendor.id, cursor: result.nextCursor, limit: 50, kind: kind.value, offsetMinutes: offset() }));
    entries.push(...result.entries);
  }
  return { ...result, entries, updatedAt: new Date().toISOString() };
}, { deps: [() => props.vendor.id, kind, () => props.mode], clearOnChange: true, coalesce: true, eventIntervalMs: 3 * 3600_000, events: [{ name: 'wallet.changed', when: event => event.vendorIds.includes(props.vendor.id) }] });
watch([() => props.vendor.id, kind, () => props.mode], () => { loadedPages.value = 1; }, { flush: 'sync' });
const rows = shallowRef<CommandData<'wallet.entries'>['entries']>([]); const cursor = ref<number | null>(); const pending = ref(false); const error = ref(''); let generation = 0;
watch(first.data, value => { generation++; rows.value = value?.entries ?? []; cursor.value = value?.nextCursor; pending.value = false; error.value = ''; });
async function more() {
  if (!cursor.value || pending.value) return;
  const current = generation; pending.value = true; error.value = '';
  try { const result = unwrap(await bridge.call('wallet.entries', { vendorId: props.vendor.id, cursor: cursor.value, limit: 50, kind: kind.value, offsetMinutes: offset() })); if (current === generation) { const ids = new Set(rows.value.map(row => row.id ?? row.seq)); rows.value = [...rows.value, ...result.entries.filter(row => !ids.has(row.id ?? row.seq))].slice(-200); loadedPages.value = Math.min(4, loadedPages.value + 1); cursor.value = result.nextCursor; } }
  catch (failure) { if (current === generation) error.value = asAppError(failure).message; }
  finally { if (current === generation) pending.value = false; }
}
const note = (entry: CommandData<'wallet.entries'>['entries'][number]) => entry.kind === 'top_up' ? '充值' : entry.kind === 'consumption_daily' ? '每日消费' : entry.note === 'calibration' ? '余额校准' : entry.note === 'migration' ? '迁移修正' : '调整 / 校准';
</script>
<template><section class="entries"><h2>本地记账资金流水</h2><div class="book-status"><span class="text-muted">上次更新：{{ first.data.value ? formatDateTime(first.data.value.updatedAt, mode) : '—' }}</span><Button :pending="first.loading.value || first.refreshing.value" @click="loadedPages = 1; first.refresh()">更新流水</Button></div><label>类型<select v-model="kind" aria-label="流水类型"><option value="all">全部类型</option><option value="consumption_daily">每日消费</option><option value="top_up">充值</option><option value="adjustment">调整与校准</option></select></label><InlineAlert v-if="first.error.value || error" severity="danger" retry @retry="first.refresh">{{ error || first.error.value?.message }}</InlineAlert><div class="table-wrap" tabindex="0" aria-label="资金流水表"><table><thead><tr><th>时间</th><th>类型</th><th>金额<HelpHint label="金额含义" :text="amountExplanation()" /></th><th>备注<HelpHint label="备注含义" :text="noteExplanation()" /></th></tr></thead><tbody><tr v-for="entry in rows" :key="entry.id ?? entry.seq"><td>{{ formatDateTime(entry.occurredAt, mode) }}</td><td>{{ note(entry) }}</td><td><Money :pico="entry.amountPico" mode="usd4" /></td><td>{{ entryNote(entry) }}</td></tr><tr v-if="!rows.length"><td colspan="4">{{ first.loading.value ? '正在读取…' : '暂无资金流水' }}</td></tr></tbody></table></div><Button v-if="cursor" :pending="pending" @click="more">加载更多</Button></section></template>
<style scoped>.entries { display: grid; gap: 12px; min-width: 0; padding: 18px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }.book-status { display:flex;align-items:center;justify-content:space-between;font-size:12px; }.table-wrap { overflow: auto; height: clamp(240px,45vh,480px); overflow-anchor: none; border: 1px solid var(--border); border-radius: 6px; }table { width: 100%; border-collapse: collapse; background: var(--surface); }th, td { padding: 9px 12px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }th { background: var(--surface-2); }th:last-child, td:last-child { white-space: normal; min-width: 240px; overflow-wrap: anywhere; }.entries > button { justify-self: start; }</style>
