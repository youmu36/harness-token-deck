<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, shallowRef } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import type { TimeZoneMode } from '../../shared/time';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import AccountEntryDetails from './AccountEntryDetails.vue';
const props = defineProps<{vendorId: string; mode: TimeZoneMode;scope?:'local'|'remote'}>();
type Entry = CommandData<'wallet.accountEntries'>['entries'][number];
const emit = defineEmits<{close: []; select: [entry: Entry, revision: number]}>();
const bridge = inject(BridgeKey)!;
const rows = shallowRef<Entry[]>([]);
const cursor = ref<string | null>(null);
const revision = ref(0);
const selected = ref('');
const loading = ref(false); const error = ref('');
const entry = computed(() => rows.value.find(row => row.id === selected.value));
let disposed = false;
onBeforeUnmount(() => { disposed = true; });
async function load(more = false) {
  if (loading.value || (more && !cursor.value)) return;
  loading.value = true; error.value = '';
  try {
    const data = unwrap(await bridge.call('wallet.accountEntries', {vendorId:props.vendorId,...(props.scope?{scope:props.scope}:{}),limit:50,cursor:more ? cursor.value : null}));
    if (disposed) return;
    if (more && data.revision !== revision.value) { error.value = '流水已变化，请重新读取后选择。'; cursor.value = null; rows.value = []; selected.value = ''; return; }
    rows.value = (more ? [...rows.value, ...data.entries] : data.entries).slice(-200);
    cursor.value = data.nextCursor; revision.value = data.revision;
    if (!rows.value.some(row => row.id === selected.value)) selected.value = '';
  } catch (failure) { if (!disposed) error.value = asAppError(failure).message; }
  finally { if (!disposed) loading.value = false; }
}
const selectable = (row: Entry) => BigInt(row.availablePico) > 0n && ['top_up','manual_top_up','consumption','interval_revision','adjustment'].includes(row.kind);
function choose() { if (entry.value && selectable(entry.value)) emit('select', entry.value, revision.value); }
void load();
</script>
<template>
  <Dialog :open="true" title="选择原资金流水" :error="error" @update:open="emit('close')">
    <p class="intro">选择需要改判的事务；区间流水按实际起止时间显示。</p>
    <Button :pending="loading" @click="load()">重新读取</Button>
    <div class="entry-list" role="radiogroup" aria-label="可改判资金流水" :aria-busy="loading">
      <label v-for="row in rows" :key="row.id" class="entry-option" :class="{selected:selected === row.id, unavailable:!selectable(row)}">
        <input v-model="selected" type="radio" name="account-entry" :value="row.id" :disabled="!selectable(row)" :aria-label="`选择流水 ${row.id}`" />
        <AccountEntryDetails :entry="row" :mode="mode" />
      </label>
      <p v-if="!rows.length">{{ loading ? '正在读取流水…' : '暂无可改判流水' }}</p>
    </div>
    <Button v-if="cursor" :pending="loading" @click="load(true)">加载更早流水</Button>
    <template #footer><Button @click="emit('close')">返回校准</Button><Button variant="primary" :disabled="!entry || loading || !selectable(entry)" @click="choose">使用此流水</Button></template>
  </Dialog>
</template>
<style scoped>
.intro{font-size:13px;margin-bottom:12px}.entry-list{display:grid;gap:10px;margin-block:12px}.entry-option{display:flex;align-items:flex-start;gap:12px;padding:14px;border:1px solid var(--border);border-radius:6px;cursor:pointer}.entry-option.selected{border-color:var(--accent)}.entry-option.unavailable{opacity:.6;cursor:default}input{margin-top:3px;flex-shrink:0}
</style>
