<script setup lang="ts">
import { inject, ref } from 'vue';
import { BridgeKey } from '../../app/ports';
import { asAppError, unwrap } from '../../app/errors';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
defineProps<{ version: string; dataDir: string }>();
const bridge = inject(BridgeKey)!; const error = ref('');
async function open() { error.value = ''; try { unwrap(await bridge.call('app.openPath', { kind: 'logDir' })); } catch (failure) { error.value = asAppError(failure).message; } }
</script>
<template><section class="settings-card" aria-label="关于"><h2>关于</h2><p>Harness Token Usage · v{{ version }}</p><p class="path">日志目录：{{ dataDir }}\logs</p><p class="text-muted">诊断日志保留最近 14 天且最多 2000 条；文件日志每份 5 MB，轮转保留 5 份。</p><p>许可证：尚未确定</p><Button @click="open">打开日志目录</Button><InlineAlert v-if="error" severity="danger">{{ error }}</InlineAlert></section></template>
