<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, provide, ref, shallowRef } from 'vue';
import { AlertTriangle, Activity } from 'lucide-vue-next';
import { BridgeKey, type CommandData } from './ports';
import { AppError, asAppError, unwrap } from './errors';
import { CLIENT_PROTOCOL, createStartupBridge } from './startup';
import Shell from './Shell.vue';
import Button from '../ui/Button.vue';
const transport = inject(BridgeKey);
if (!transport) throw new Error('StartupGate requires an application bridge');
const guard = createStartupBridge(transport);
provide(BridgeKey, guard.bridge);
const hello = shallowRef<CommandData<'app.hello'>>();
const preferences = shallowRef<CommandData<'preferences.get'>>();
const error = shallowRef<AppError>();
const pending = ref(true);
const logPath = computed(() => hello.value?.dataDir
  ? `${hello.value.dataDir.replace(/[\\/]$/, '')}\\logs\\htu.log`
  : new URLSearchParams(location.hash.split('?')[1]).get('logPath') ?? '本机账本目录下的 logs/htu.log');
const incompatible = computed(() => error.value?.code === 'PROTOCOL_MISMATCH' || error.value?.code === 'CONTRACT_MISMATCH');
let generation = 0;
async function start() {
  const current = ++generation;
  pending.value = true; error.value = undefined; preferences.value = undefined; guard.setWritable(false);
  transport!.host?.setReady(false);
  try {
    const response = unwrap(await transport!.call('app.hello', { clientProtocol: CLIENT_PROTOCOL }));
    if (current !== generation) return;
    hello.value = response;
    if (Number(response.protocol) !== CLIENT_PROTOCOL) throw new AppError({ code: 'PROTOCOL_MISMATCH', message: '界面与应用版本不兼容', retryable: false });
    const snapshot = unwrap(await guard.bridge.call('preferences.get', {}));
    if (current !== generation) return;
    preferences.value = snapshot;
    guard.setWritable(true);
    transport!.host?.setReady(true);
  } catch (failure) {
    if (current === generation) error.value = asAppError(failure);
  } finally { if (current === generation) pending.value = false; }
}
onMounted(() => { void start(); });
onBeforeUnmount(() => { generation++; guard.setWritable(false); });
</script>

<template>
  <div v-if="pending" class="startup" role="status"><Activity :size="30" aria-hidden="true" /><h1>Harness Token Usage</h1><p class="text-muted">正在连接本机账本…</p></div>
  <main v-else-if="error" class="startup">
    <div class="failure surface" role="alert">
      <AlertTriangle :size="32" class="danger" aria-hidden="true" />
      <h1>{{ incompatible ? '界面与应用版本不兼容' : '启动失败' }}</h1>
      <p>{{ error.message }}</p>
      <p v-if="incompatible" class="text-muted">请更新应用和界面后重新启动。</p>
      <p class="text-muted">如数据库损坏，请使用备份恢复账本，或保留数据目录后重新安装应用。</p>
      <p class="log-path">日志：{{ logPath }}</p>
      <div class="actions"><Button variant="primary" @click="start">重试</Button>
        <Button :disabled="!transport.host" @click="transport.host?.openLogDir()">打开日志目录</Button>
        <Button :disabled="!transport.host" @click="transport.host?.quit()">退出</Button></div>
      <p v-if="!transport.host" class="text-muted">浏览器预览可刷新重试或关闭当前标签页。</p>
    </div>
  </main>
  <Shell v-else-if="hello && preferences" :hello="hello" :initial-preferences="preferences" />
</template>

<style scoped>
.startup { min-height: 100vh; padding: 48px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; }
.failure { max-width: 660px; padding: 32px; display: grid; gap: 16px; }
.danger { color: var(--danger); }
.log-path { overflow-wrap: anywhere; font-size: 12px; color: var(--text-muted); }
.actions { display: flex; gap: 8px; }
</style>
