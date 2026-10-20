<script setup lang="ts">
import { ref } from 'vue';
import type { CommandData } from '../../app/ports';
import type { DeepReadonly } from 'vue';
import { useCommand } from '../../shared/command';
import { asAppError } from '../../app/errors';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ groupId: string; vendorId?: string | null; wallet?: DeepReadonly<CommandData<'wallet.list'>>; leave: (action: () => void) => void }>();
const emit = defineEmits<{ saved: [] }>();
const command = useCommand('wallet.linkGroups'); const error = ref('');
function change(event: Event) { const select = event.target as HTMLSelectElement; const id = select.value; select.value = props.vendorId ?? ''; props.leave(() => { void save(id); }); }
async function save(id: string) {
  const vendor = props.wallet?.vendors.find(v => v.id === (id || props.vendorId)); if (!vendor || !props.wallet) return;
  error.value = '';
  try { await command.run({ vendorId: vendor.id, groupIds: id ? [...new Set([...vendor.linkedGroups, props.groupId])] : vendor.linkedGroups.filter(g => g !== props.groupId), expectedRevision: props.wallet.revision }); emit('saved'); }
  catch (failure) { error.value = asAppError(failure).message; }
}
</script>
<template><div class="group-vendor"><label>关联供应商<select :value="vendorId ?? ''" :disabled="!wallet || command.pending.value" aria-label="价格组关联供应商" @change="change"><option value="">未关联</option><option v-for="vendor in wallet?.vendors.filter(v => v.status === 'active')" :key="vendor.id" :value="vendor.id">{{ vendor.name }}</option></select></label><InlineAlert v-if="error" severity="danger">{{ error }}</InlineAlert></div></template>
<style scoped>.group-vendor label { display: flex; gap: 12px; align-items: center; font-size: 13px; }.group-vendor select { min-width: 160px; padding: 8px; border-radius: 6px; }</style>
