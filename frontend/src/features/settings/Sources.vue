<script setup lang="ts">
import { inject } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import type { TimeZoneMode } from '../../shared/time';
import SourceCard from './SourceCard.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
const bridge = inject(BridgeKey)!;
withDefaults(defineProps<{ mode?: TimeZoneMode }>(), { mode: 'system' });
const query = useQuery('sources.list', () => bridge.call('sources.list', {}).then(unwrap), { events: ['sources.changed'] });
</script>
<template><section class="settings-card" aria-label="采集来源"><h2>采集来源</h2><p class="text-muted">更换路径会保留采集起点和已有去重记录。自动定位会采用各来源的有效默认路径。</p><SourceCard v-for="source in query.data.value?.sources" :key="source.harness" :source="source" :mode="mode" :revision="query.data.value!.revision" :refresh="query.refresh" /><p v-if="query.loading.value">正在读取来源…</p><InlineAlert v-if="query.error.value" severity="danger" retry @retry="query.refresh">{{ query.error.value.message }}</InlineAlert></section></template>
