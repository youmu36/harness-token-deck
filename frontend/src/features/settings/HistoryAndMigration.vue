<script setup lang="ts">
import { inject } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import Button from '../../ui/Button.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const bridge = inject(BridgeKey)!;
defineEmits<{ history: []; migration: [] }>();
const detection = useQuery('settings.migration', () => bridge.call('migration.detect', {}).then(unwrap), { events: ['usage.changed', 'wallet.changed'] });
</script>
<template><section class="settings-card" aria-label="历史与迁移"><h2>旧版数据导入</h2><p class="text-muted">保留旧版数据库的用量、钱包和价格配置。</p><div class="settings-actions"><Button @click="$emit('migration')">导入旧版数据…</Button></div><InlineAlert v-if="detection.error.value" severity="danger" retry @retry="detection.refresh()">{{ detection.error.value.message }}</InlineAlert></section></template>
