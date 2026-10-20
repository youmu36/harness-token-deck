<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue';
import { BridgeKey, type CommandData, type CommandPayload } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useCommand } from '../../shared/command';
import { formatRemoteAmount, kindLabels, type RemoteSnapshot } from './remote';
import Dialog from '../../ui/Dialog.vue';
import Button from '../../ui/Button.vue';
const props = defineProps<{ vendorId: string; vendorName: string; vendorUrl?: string | null }>();
const emit = defineEmits<{ close: []; saved: [] }>();
const bridge = inject(BridgeKey)!;
const saved = ref<RemoteSnapshot>(); const loading = ref(true); const error = ref('');
const baseUrl = ref(props.vendorUrl ?? ''); const authMode = computed(() => siteType.value === 'newapi' ? 'access_token' : 'api_key');
const siteType = ref<'' | 'newapi' | 'sub2api' | 'deepseek'>('');
const userId = ref(''); const credential = ref(''); const quotaPerUnit = ref('');
const refreshMinutes = ref(5); const enabled = ref(true); const testing = ref(false);
const result = ref<CommandData<'remote.test'>>(); const confirmDelete = ref(false);
const save = useCommand('remote.save'); const remove = useCommand('remote.delete');
const pending = computed(() => save.pending.value || remove.pending.value || testing.value);
const outcome = ref<HTMLElement>(); const form = ref<HTMLFormElement>(); const attempted = ref(false);
const needsCredential = computed(() => !saved.value?.account?.maskedCredential || saved.value.account.siteType !== siteType.value || saved.value.account.authMode !== authMode.value || saved.value.account.userId !== (siteType.value === 'newapi' ? userId.value : null));
const normalizedCredential = computed(() => credential.value.trim().replace(/^Bearer\s+/i, '').trim());
const validUser = computed(() => /^[0-9]{1,20}$/.test(userId.value) && BigInt(userId.value) > 0n);
const missing = computed(() => [!baseUrl.value.trim() && '站点地址', !siteType.value && '接口类型', siteType.value === 'newapi' && !userId.value && '用户 ID', needsCredential.value && !normalizedCredential.value && (siteType.value === 'newapi' ? '访问令牌' : 'API 令牌')].filter(Boolean));
async function scrollOutcome() { await nextTick(); const body = outcome.value?.closest('.dialog__body'); body?.scrollTo({ top: body.scrollHeight, behavior: 'smooth' }); }
async function validate() {
  attempted.value = true;
  if (missing.value.length) error.value = `有必填项未完成：${missing.value.join('、')}。请填写标有 * 的项目。`;
  else if (siteType.value === 'newapi' && !validUser.value) error.value = '用户 ID 须为不超过 20 位的正整数。';
  else if (siteType.value === 'newapi' && quotaPerUnit.value && !/^[1-9][0-9]{0,39}$/.test(quotaPerUnit.value)) error.value = '换算系数须为正整数。';
  else if (!Number.isInteger(refreshMinutes.value) || refreshMinutes.value < 1 || refreshMinutes.value > 60) error.value = '刷新间隔须为 1 至 60 分钟的整数。';
  else { error.value = ''; return true; }
  await nextTick(); form.value?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(); await scrollOutcome(); return false;
}
let generation = 0;
watch(siteType, value => { if (value === 'deepseek') baseUrl.value = 'https://api.deepseek.com'; });
watch([baseUrl, siteType, authMode, userId, credential, quotaPerUnit, refreshMinutes, enabled], () => { ++generation; result.value = undefined; error.value = ''; confirmDelete.value = false; });
function payload(): CommandPayload<'remote.save'> { return { vendorId: props.vendorId, baseUrl: baseUrl.value.trim().replace(/\/$/, ''), siteType: siteType.value as 'newapi' | 'sub2api' | 'deepseek', authMode: authMode.value, userId: authMode.value === 'access_token' ? userId.value : null, credential: normalizedCredential.value || null, quotaPerUnit: siteType.value === 'newapi' ? quotaPerUnit.value || null : null, refreshMinutes: refreshMinutes.value, enabled: enabled.value }; }
async function load() {
  loading.value = true; error.value = '';
  try { saved.value = unwrap(await bridge.call('remote.get', { vendorId: props.vendorId })); const a = saved.value.account; if (a) { baseUrl.value = a.baseUrl; siteType.value = a.siteType === 'auto' ? '' : a.siteType; userId.value = a.userId ?? ''; quotaPerUnit.value = a.quotaPerUnit ?? ''; refreshMinutes.value = a.refreshMinutes; enabled.value = a.enabled; } }
  catch (failure) { error.value = asAppError(failure).message; }
  finally { loading.value = false; }
}
void load();
async function test() { if (pending.value || !await validate()) return; testing.value = true; result.value = undefined; const current = generation; try { const response = unwrap(await bridge.call('remote.test', payload())); if (current === generation) result.value = response; } catch (failure) { if (current === generation) error.value = asAppError(failure).message; } finally { testing.value = false; if (current === generation) await scrollOutcome(); } }
async function submit() { if (pending.value || !await validate()) return; try { await save.run(payload()); credential.value = ''; emit('saved'); emit('close'); } catch (failure) { error.value = asAppError(failure).message; await scrollOutcome(); } }
async function deleteAccount() { if (!confirmDelete.value) { confirmDelete.value = true; return; } error.value = ''; try { await remove.run({ vendorId: props.vendorId }); credential.value = ''; emit('saved'); emit('close'); } catch (failure) { error.value = asAppError(failure).message; } }
</script>
<template>
  <Dialog :open="true" title="远程账户" :pending="pending" @update:open="emit('close')">
    <p v-if="loading">正在读取远程账户…</p>
    <form v-else id="remote-account" ref="form" class="fields" novalidate @submit.prevent="submit">
      <p>{{ vendorName }} · 测试连接不改变本地钱包；保存后按余额偏差校准，符合大于 1 USD 的条件才自动补记充值。</p>
      <p class="text-muted">* 为必填项。</p><label>站点地址 *<input v-model="baseUrl" aria-label="站点地址" aria-required="true" :aria-invalid="attempted && !baseUrl.trim()" placeholder="https://example.com（站点根地址）" :disabled="pending" /></label>
      <p v-if="/\/v1\/?$/.test(baseUrl)" class="text-muted">通常应填写站点根地址；/v1 是模型调用路径，可能不提供余额接口。</p>
      <p v-if="baseUrl.startsWith('http://')" class="warning">HTTP 仅允许 localhost / 127.0.0.1，本地请求未加密。</p>
      <label>接口类型 *<select v-model="siteType" aria-label="接口类型" aria-required="true" :aria-invalid="attempted && !siteType" :disabled="pending"><option value="" disabled>请选择接口类型</option><option value="sub2api">sub2api</option><option value="newapi">New API</option><option value="deepseek">DeepSeek 官方</option></select></label>
      <p v-if="saved?.account?.siteType === 'auto'" class="text-muted">请选择实际接口类型并确认凭据，旧配置不会再自动识别接口。</p>
      <label v-if="authMode === 'access_token'">用户 ID *<input v-model="userId" aria-label="用户 ID" aria-required="true" :aria-invalid="attempted && !validUser" inputmode="numeric" :disabled="pending" /></label>
      <label>{{ authMode === 'access_token' ? '访问令牌' : siteType === 'deepseek' ? 'API 密钥' : 'API 令牌' }}{{ needsCredential ? ' *' : '（可选更新）' }}<input v-model="credential" type="password" aria-label="令牌" :aria-required="needsCredential" :aria-invalid="attempted && needsCredential && !normalizedCredential" autocomplete="new-password" :disabled="pending" :placeholder="!needsCredential ? '留空不修改已保存令牌' : authMode === 'access_token' ? '输入系统访问令牌' : '输入 API 令牌'" maxlength="4096" /></label>
      <p v-if="saved?.account?.maskedCredential" class="text-muted">已保存 {{ saved.account.maskedCredential }}，{{ needsCredential ? '查询身份已变更，请重新填写对应令牌。' : '留空不修改。' }}</p>
      <label v-if="siteType === 'newapi'">换算系数（可选覆盖）<input v-model="quotaPerUnit" aria-label="换算系数" inputmode="numeric" placeholder="由 /api/status 自动获取" :disabled="pending" /></label>
      <p v-if="siteType === 'deepseek'" class="text-muted">官方查询仍需 API 密钥进行 Bearer 认证，无需用户 ID 或换算系数。余额以 1:1 的比例从 CNY 转换为 USD 进行计算。</p>
      <p v-else-if="siteType === 'sub2api'" class="text-muted">使用 API 令牌直接查询 sub2api 账户余额。</p>
      <p v-else-if="siteType === 'newapi'" class="text-muted">使用访问令牌和用户 ID 查询 New API 账户余额；API 密钥不能用于此查询。</p>
      <label class="check"><input v-model="enabled" type="checkbox" :disabled="pending" />自动刷新</label>
      <label v-if="enabled">刷新间隔（分钟） *<input v-model.number="refreshMinutes" type="number" aria-label="刷新间隔" aria-required="true" :aria-invalid="attempted && (!Number.isInteger(refreshMinutes) || refreshMinutes < 1 || refreshMinutes > 60)" min="1" max="60" :disabled="pending" /><span class="text-muted">{{ refreshMinutes < 5 ? '低于 5 分钟，失败后等待下个同步周期，不自动重试。' : '同步失败后每隔 10 秒重试，最多两次。' }}</span></label>
      <Button :pending="testing" :disabled="pending" @click="test">测试连接</Button>
      <div ref="outcome" class="outcome" aria-live="polite"><p v-if="error" class="error" role="alert">{{ error }}</p><div v-if="result" class="test-result"><p v-if="result.error" class="warning">测试失败：{{ result.error.message }}</p><template v-else-if="result.observation"><p>连接成功 · {{ kindLabels[result.observation.kind] }}</p><span v-if="result.observation.amountPico !== null" class="money">{{ formatRemoteAmount(result.observation) }}</span><p v-else-if="result.observation.kind === 'unlimited'">不限额（无法据此查询账户总余额）</p><p v-else-if="result.observation.kind === 'quota_units'">{{ result.observation.rawQuota }} 配额单位</p><p v-if="!result.observation.canCalibrate" class="text-muted">此结果不能用于一键校准。</p></template></div>
      <p v-if="confirmDelete" class="warning">确认移除远程账户和已保存令牌？本地钱包记录保留。</p>
    </div></form>
    <template #footer><Button v-if="saved?.account" variant="danger" :disabled="pending" @click="deleteAccount">{{ confirmDelete ? '确认移除' : '移除账户' }}</Button><Button :disabled="pending" @click="emit('close')">取消</Button><Button variant="primary" type="submit" form="remote-account" :pending="save.pending.value" :disabled="pending || loading">保存</Button></template>
  </Dialog>
</template>
<style scoped>.fields { display: grid; gap: 14px; }label { display: grid; gap: 6px; }input:not([type=checkbox]), select { width: 100%; padding: 7px 10px; border-radius: 6px; }.check { display: flex; gap: 8px; align-items: center; }.outcome { display: grid; gap: 8px; }.error { color: var(--danger); }[aria-invalid=true] { border-color: var(--danger); }.warning { color: var(--warn); }.money { color: var(--money); font-variant-numeric: tabular-nums; }.test-result { padding: 12px; background: var(--surface-2); border-radius: 6px; display: grid; gap: 8px; }</style>

