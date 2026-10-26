<script setup lang="ts">
import { inject, ref } from 'vue';
import { BridgeKey } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import { useCommand } from '../../shared/command';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import { useShellPreferences } from '../../app/useShellPreferences';
import type { CommandData } from '../../app/ports';
const props = defineProps<{ initial: CommandData<'preferences.get'> }>();
const preferences = useShellPreferences(props.initial);
const saving = ref(false);
async function setting(key: 'closeToTray' | 'startAtLogin', event: Event) {
  const input = event.target as HTMLInputElement; const value = input.checked;
  saving.value = true;
  try { await preferences.save({ [key]: value }); }
  catch { input.checked = !value; }
  finally { saving.value = false; }
}
const bridge = inject(BridgeKey)!;
const query = useQuery('collection.status', () => bridge.call('collection.status', {details:false}).then(unwrap), { events: ['sources.changed', 'collection.tick'] });
const command = useCommand('collection.setPaused'); const error = ref('');
async function toggle() { error.value = ''; try { await command.run({ paused: !query.data.value!.paused }); await query.refresh(); } catch (failure) { error.value = asAppError(failure).message; } }
</script>
<template><section class="settings-card" aria-label="后台"><h2>后台运行</h2><div class="settings-actions"><p role="status">{{ query.data.value?.paused ? '采集已暂停' : query.data.value?.state === 'scanning' ? '正在采集' : query.data.value ? '采集运行中' : '正在读取采集状态…' }}</p><Button :pending="command.pending.value" :disabled="!query.data.value" @click="toggle">{{ query.data.value?.paused ? '恢复采集' : '暂停采集' }}</Button></div><label class="checkbox-field"><input type="checkbox" :checked="preferences.snapshot.value.preferences.closeToTray ?? true" :disabled="saving" @change="setting('closeToTray', $event)" />关闭窗口后进入托盘</label><p class="text-muted">勾选后关闭窗口会继续在后台采集；取消勾选后，关闭窗口将退出软件。</p><label class="checkbox-field"><input type="checkbox" :checked="preferences.snapshot.value.preferences.startAtLogin ?? false" :disabled="saving" @change="setting('startAtLogin', $event)" />开机自启</label><p class="text-muted">登录 Windows 后自动启动本软件。暂停期间仍会清理到期数据与轮转日志。</p><InlineAlert v-if="error || query.error.value || preferences.error.value" severity="danger">{{ error || query.error.value?.message || preferences.error.value }}</InlineAlert></section></template>
