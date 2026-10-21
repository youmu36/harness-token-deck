<script setup lang="ts">
import { computed, inject, ref } from 'vue';
import { BridgeKey } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useCommand } from '../../shared/command';
import { formatMoney } from '../../shared/money';
import { useToast } from '../../ui/useToast';
import type { Catalog } from '../harness/types';
import type { Vendor, WalletAction } from './types';
import { amountPico } from './types';
import { remoteAmount, kindLabels, type RemoteObservation } from './remote';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
import Money from '../../ui/Money.vue';
const props = defineProps<{ action: WalletAction; vendor?: Vendor; catalog?: Catalog; walletRevision: number; refresh: () => Promise<void>; latest: () => Vendor | undefined; remoteObservation?: RemoteObservation; walletScope?: 'local'|'remote'; hidePast?: boolean }>();
const emit = defineEmits<{ close: []; saved: [vendorId: string] }>();
const commands = { create: useCommand('wallet.createVendor'), topup: useCommand('wallet.topUp'), calibrate: useCommand('wallet.calibrate'), link: useCommand('wallet.linkGroups'), rename: useCommand('wallet.renameVendor'), delete: useCommand('wallet.deleteVendor'), restore: useCommand('wallet.restoreVendor') };
const pending = computed(() => commands[props.action].pending.value);
const title = { create: '添加供应商', topup: '充值', calibrate: '校准余额', link: '关联价格组', rename: '编辑供应商', delete: '删除供应商', restore: '恢复供应商' };
const url = ref(props.vendor?.url ?? '');
const pastInput = (value?: string) => !value || BigInt(value) === 0n ? '' : remoteAmount(value);
const pastTopUp = ref(pastInput(props.vendor?.pastTopUpPico));
const pastConsumption = ref(pastInput(props.vendor?.pastConsumptionPico));
const originalPast = { topUp: pastTopUp.value, consumption: pastConsumption.value };
const pastValid = computed(() => [pastTopUp.value, pastConsumption.value].every(amount => { const value = amountPico(amount || '0'); return value !== undefined && value >= 0n; }));
const name = ref(props.vendor?.name ?? ''); const amount = ref(props.remoteObservation?.amountPico ? remoteAmount(props.remoteObservation.amountPico) : ''); const error = ref('');
const localVendor = ref(props.vendor); const groupIds = ref([...(props.vendor?.linkedGroups ?? [])]);
const revision = ref(props.walletRevision); const toast = useToast();
const parsed = computed(() => amountPico(amount.value));
const remoteSelected = computed(() => props.remoteObservation?.amountPico != null && amount.value === remoteAmount(props.remoteObservation.amountPico));
const delta = computed(() => parsed.value === undefined || !localVendor.value ? undefined : (remoteSelected.value ? BigInt(props.remoteObservation!.amountPico!) : parsed.value) - BigInt(localVendor.value.account?.B ?? localVendor.value.B));
const signedDelta = computed(() => delta.value === undefined ? '---' : `${delta.value >= 0n ? '+' : ''}${formatMoney(delta.value, 'usd4')}`);
const valid = computed(() => props.action === 'create' ? !!name.value.trim() && name.value.trim().length <= 60 && (!amount.value || parsed.value !== undefined)
  : props.action === 'rename' ? !!name.value.trim() && name.value.trim().length <= 60
  : props.action === 'topup' ? parsed.value !== undefined && parsed.value > 0n
  : props.action === 'calibrate' ? parsed.value !== undefined : true);
const hasEntries = ref<boolean>(); const checking = ref(props.action === 'delete');
const bridge = inject(BridgeKey)!;
async function checkEntries() { checking.value = true; error.value = ''; try { hasEntries.value = unwrap(await bridge.call('wallet.entries', { vendorId: props.vendor!.id, limit: 1 })).entries.length > 0; } catch (failure) { error.value = asAppError(failure).message; } finally { checking.value = false; } }
if (props.action === 'delete') void checkEntries();
async function submit() {
  if (!valid.value || (['create', 'rename'].includes(props.action) && !pastValid.value) || pending.value || checking.value || (props.action === 'delete' && hasEntries.value === undefined)) return;
  error.value = '';
  try {
    let vendorId = props.vendor?.id ?? '';
    if (props.action === 'create') vendorId = (await commands.create.run({ name: name.value.trim(), url: url.value.trim() || null, initialBalance: amount.value || null, pastTopUp: pastTopUp.value || '0', pastConsumption: pastConsumption.value || '0' })).vendorId;
    else if (props.action === 'topup') { await commands.topup.run({ vendorId, amount: amount.value, ...(props.walletScope ? {walletScope:props.walletScope} : {}) }); toast.success(`已充值 ${formatMoney(parsed.value!, 'usd4')}`); }
    else if (props.action === 'calibrate') await commands.calibrate.run({ vendorId, ...(props.walletScope === 'remote' ? {walletScope:'remote' as const} : {}), target: amount.value, expectedWalletRevision: localVendor.value!.walletRevision, ...(remoteSelected.value ? { remoteFetchedAt: props.remoteObservation!.fetchedAt } : {}) });
    else if (props.action === 'link') await commands.link.run({ vendorId, groupIds: groupIds.value, expectedRevision: revision.value });
    else if (props.action === 'rename') await commands.rename.run({ vendorId, name: name.value.trim(), ...(!props.hidePast && pastTopUp.value !== originalPast.topUp ? {pastTopUp: pastTopUp.value || '0'} : {}), ...(!props.hidePast && pastConsumption.value !== originalPast.consumption ? {pastConsumption: pastConsumption.value || '0'} : {}), ...(url.value !== (props.vendor?.url ?? '') ? { url: url.value.trim() || null } : {}) });
    else if (props.action === 'delete') await commands.delete.run({ vendorId });
    else await commands.restore.run({ vendorId });
    emit('saved', vendorId); emit('close');
  } catch (failure) {
    const issue = asAppError(failure); error.value = issue.message;
    if (issue.code === 'WALLET_REVISION_CONFLICT') {
      await props.refresh(); localVendor.value = props.latest(); revision.value = localVendor.value?.walletRevision ?? revision.value;
      error.value = props.action === 'calibrate' && localVendor.value ? `余额已变化（新余额 ${formatMoney(localVendor.value.B, 'usd4')}），请确认后重试` : '关联已变化，请重新确认后重试';
    }
  }
}
</script>
<template>
  <Dialog :open="true" :title="title[action]" :pending="pending" :error="error" @update:open="emit('close')">
    <form id="wallet-command" class="fields" @submit.prevent="submit">
      <p v-if="vendor">{{ vendor.name }}</p>
      <label v-if="action === 'create' || action === 'rename'">名称<input v-model="name" aria-label="名称" maxlength="60" :disabled="pending" /></label>
      <label v-if="action === 'create' || (action === 'rename' && !hidePast)">过往充值金额（USD，选填）<input v-model="pastTopUp" aria-label="过往充值金额" inputmode="decimal" :disabled="pending" placeholder="留空按 0" /><span class="text-muted">记录使用本软件前的充值，可随时修改；单独展示，不改变当前余额或资金流水。</span><span v-if="!pastValid" role="alert">请输入非负金额，最多 6 位小数。</span></label>
      <label v-if="action === 'create' || (action === 'rename' && !hidePast)">过往消费金额（USD，选填）<input v-model="pastConsumption" aria-label="过往消费金额" inputmode="decimal" :disabled="pending" placeholder="留空按 0" /><span class="text-muted">记录使用本软件前的消费，可随时修改；单独展示，不改变当前余额或资金流水。</span></label>
      <label v-if="action === 'create' || action === 'rename'">供应商网址（选填）<input v-model="url" aria-label="供应商网址" maxlength="2048" :disabled="pending" placeholder="https://example.com" /><span class="text-muted">将作为远程账户的站点地址；已配置的站点请在远程账户中修改。</span></label>
      <p v-if="action === 'calibrate'">当前显示余额：<Money v-if="localVendor" :pico="localVendor.account?.B ?? localVendor.B" mode="usd4" /></p>
      <p v-if="remoteObservation">远程来源：{{ remoteObservation.source }} · {{ kindLabels[remoteObservation.kind] }} · 获取于 {{ remoteObservation.fetchedAt }}；远程额度 <Money v-if="remoteObservation.amountPico !== null" :pico="remoteObservation.amountPico" mode="usd4" /></p>
      <p v-if="remoteSelected" class="text-muted">确认后按获取时的完整精度金额校准。</p>
      <label v-if="['create', 'topup', 'calibrate'].includes(action)">{{ action === 'create' ? '当前余额（可选）' : action === 'topup' ? '本次充值（USD）' : '实际余额（USD）' }}<input v-model="amount" aria-label="金额" inputmode="decimal" :disabled="pending" placeholder="最多 6 位小数" /></label>
      <p v-if="action === 'topup' && walletScope === 'local'" class="text-muted">本次金额将增加到本地累计充值。本地数据静默十分钟后生成充值流水。</p>
      <p v-if="action === 'calibrate'" aria-live="polite">将追加调整 {{ signedDelta }}</p>
      <template v-if="action === 'link'"><p>关联只影响之后入账的请求，已计入的消费不会转移。</p><label v-for="group in catalog?.groups.filter(g => !g.historical)" :key="group.id" class="check"><input v-model="groupIds" type="checkbox" :value="group.id" :disabled="pending" />{{ group.name }}<span v-if="group.vendorId && group.vendorId !== vendor?.id" class="text-muted">（已关联其他供应商，将改为此供应商）</span></label></template>
      <p v-if="action === 'delete'">{{ checking ? '正在检查资金记录…' : hasEntries === undefined ? '资金记录读取失败' : hasEntries ? '该供应商有资金记录，将归档：历史保留只读，并解除所有价格组关联' : '删除后不可恢复' }}</p>
      <Button v-if="action === 'delete' && hasEntries === undefined && !checking" @click="checkEntries">重新读取资金记录</Button>
      <p v-if="action === 'restore'">恢复后不还原原来的价格组关联。</p>
    </form>
    <template #footer><Button :disabled="pending" @click="emit('close')">取消</Button><Button form="wallet-command" type="submit" :variant="action === 'delete' ? 'danger' : 'primary'" :pending="pending" :disabled="!valid || (['create', 'rename'].includes(action) && !pastValid) || checking || (action === 'delete' && hasEntries === undefined)">{{ action === 'delete' ? hasEntries ? '确认归档' : '确认删除' : '确认' }}</Button></template>
  </Dialog>
</template>
<style scoped>.fields { display: grid; gap: 14px; }label { display: grid; gap: 6px; }input:not([type=checkbox]) { width: 100%; padding: 7px 10px; border-radius: 6px; }.check { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 13px; }</style>
