<script setup lang="ts">
import { computed, nextTick, ref } from 'vue';
import type { Group, Version } from './types';
import { harnessNames, versionName } from '../harness/types';
import { useCommand } from '../../shared/command';
import { asAppError } from '../../app/errors';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ action: 'create' | 'rename' | 'copy' | 'disable' | 'delete' | 'addVersion' | 'label' | 'activate' | 'disableVersion' | 'deleteVersion'; group?: Group; version?: Version; revision: number; refresh: () => Promise<void> }>();
const emit = defineEmits<{ close: []; saved: [groupId: string | null, versionId: string | null] }>();
const name = ref(props.action === 'copy' ? `${props.group?.name ?? ''} 副本` : props.action === 'label' ? props.version?.label ?? '' : props.action === 'rename' ? props.group?.name ?? '' : '');
const number = ref(props.group?.nextVersionNumber ?? 1);
const copyId = ref(''); const error = ref(''); const revision = ref(props.revision);
const commands = { create: useCommand('pricing.createGroup'), rename: useCommand('pricing.renameGroup'), copy: useCommand('pricing.createGroup'), disable: useCommand('pricing.setGroupDisabled'), delete: useCommand('pricing.deleteGroup'), addVersion: useCommand('pricing.addVersion'), label: useCommand('pricing.setVersionLabel'), activate: useCommand('pricing.activateVersion'), disableVersion: useCommand('pricing.setVersionDisabled'), deleteVersion: useCommand('pricing.deleteVersion') };
const pending = computed(() => commands[props.action].pending.value);
const titles = { create: '新建价格组', rename: '重命名价格组', copy: '复制为新组', disable: props.group?.disabled ? '启用价格组' : '停用价格组', delete: '删除价格组', addVersion: '新增版本', label: '重命名显示名', activate: '设为生效版本', disableVersion: props.version?.disabled ? '启用价格版本' : '停用价格版本', deleteVersion: '删除价格版本' };
const valid = computed(() => ['create', 'rename', 'copy'].includes(props.action) ? !!name.value.trim() && name.value.trim().length <= 60 : props.action === 'label' ? name.value.length <= 60 : props.action === 'addVersion' ? Number.isInteger(number.value) && number.value > 0 && number.value <= 4294967295 && name.value.length <= 60 : true);
async function submit() {
  if (!valid.value || pending.value) return; error.value = '';
  try {
    const expectedRevision = revision.value; const groupId = props.group?.id ?? ''; const versionId = props.version?.id ?? '';
    let result;
    if (props.action === 'create' || props.action === 'copy') result = await commands[props.action].run({ name: name.value.trim(), copyFromGroupId: props.action === 'copy' ? groupId : null, expectedRevision });
    else if (props.action === 'rename') result = await commands.rename.run({ groupId, name: name.value.trim(), expectedRevision });
    else if (props.action === 'disable') result = await commands.disable.run({ groupId, disabled: !props.group!.disabled, expectedRevision });
    else if (props.action === 'delete') result = await commands.delete.run({ groupId, expectedRevision });
    else if (props.action === 'addVersion') result = await commands.addVersion.run({ groupId, number: number.value, label: name.value.trim(), copyFromVersionId: copyId.value || null, expectedRevision });
    else if (props.action === 'disableVersion') result = await commands.disableVersion.run({ versionId, disabled: !props.version?.disabled, expectedRevision });
    else if (props.action === 'deleteVersion') result = await commands.deleteVersion.run({ versionId, expectedRevision });
    else if (props.action === 'label') result = await commands.label.run({ versionId, label: name.value.trim(), expectedRevision });
    else result = await commands.activate.run({ groupId, versionId, expectedRevision });
    emit('saved', result.groupId, result.versionId); emit('close');
  } catch (failure) { const issue = asAppError(failure); error.value = issue.message; if (issue.code === 'PRICING_REVISION_CONFLICT') { await props.refresh(); await nextTick(); revision.value = props.revision; error.value = '价格目录已变化，请检查最新目录并重新确认'; } }
}
</script>
<template><Dialog :open="true" :title="titles[action]" :pending="pending" :error="error" @update:open="emit('close')"><form id="pricing-command" class="fields" @submit.prevent="submit"><label v-if="['create','rename','copy','label','addVersion'].includes(action)">{{ action === 'label' || action === 'addVersion' ? '显示名（可选）' : '名称' }}<input v-model="name" aria-label="名称" maxlength="60" :disabled="pending" /></label><template v-if="action === 'addVersion'"><label>版本号<input v-model.number="number" type="number" aria-label="版本号" min="1" max="4294967295" step="1" :disabled="pending" /></label><label>复制价格自<select v-model="copyId" aria-label="复制价格自" :disabled="pending"><option value="">空版本</option><option v-for="v in group?.versions" :key="v.id" :value="v.id">{{ versionName(v) }}</option></select></label></template><p v-if="action === 'activate'">将 {{ group?.name }} · {{ version ? versionName(version) : '' }} 设为生效版本。受影响 Harness：{{ group?.harnesses.map(h => harnessNames[h]).join('、') || '无（此组尚未使用）' }}。已入账记录不会自动重算。</p><p v-if="action === 'delete'">删除此组及全部版本后不可恢复。</p><p v-if="action === 'disable'">{{ group?.disabled ? '启用后可编辑并供 Harness 选择。' : '停用后价格编辑器只读。' }}</p><p v-if="action === 'deleteVersion'">删除此版本后不可恢复，历史价格与计费记录保留；不再用于新请求或编辑。</p><p v-if="action === 'disableVersion'">{{ version?.disabled ? '启用后可编辑并设为生效。' : '停用后保留历史，停止新的选择与价格编辑。' }}</p><p v-if="action === 'copy'">复制当前生效版本的价格，不复制供应商关联。</p></form><template #footer><Button :disabled="pending" @click="emit('close')">取消</Button><Button form="pricing-command" type="submit" :pending="pending" :disabled="!valid" :variant="action === 'delete' || action === 'deleteVersion' ? 'danger' : 'primary'">确认</Button></template></Dialog></template>
<style scoped>.fields { display: grid; gap: 14px; }label { display: grid; gap: 6px; }input, select { width: 100%; padding: 7px 10px; border-radius: 6px; }</style>
