<script setup lang="ts">
import { inject, ref, watch }  from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
import Display from './Display.vue';
import Sources from './Sources.vue';
import Background from './Background.vue';
import Data from './Data.vue';
import HistoryAndMigration from './HistoryAndMigration.vue';
import About from './About.vue';
import InlineAlert from '../../ui/InlineAlert.vue';
import Button from '../../ui/Button.vue';
import SetupGuideDialog from './SetupGuideDialog.vue';
import SectionNav from '../../ui/SectionNav.vue';
import { useShellPreferences } from '../../app/useShellPreferences';
import type { CommandData } from '../../app/ports';
type PreferenceValues = CommandData<'preferences.get'>['preferences'];
const props = defineProps<{ initial: CommandData<'preferences.get'>; background?: boolean }>();
const saved = useShellPreferences(props.initial);
const section = ref<PreferenceValues['settingsSection']>(props.background ? 'background' : saved.snapshot.value.preferences.settingsSection ?? 'display');
watch(() => props.background, value => { if (value) section.value = 'background'; });
const sections = [{ value: 'display', label: '显示' }, { value: 'sources', label: '采集来源' }, { value: 'background', label: '后台运行' }, { value: 'data', label: '数据与历史' }, { value: 'about', label: '指南与关于' }];
watch(section, value => { void saved.save({ settingsSection: value }).catch(() => {}); });
const guideOpen = ref(false);
function guideNavigate(path: string) { guideOpen.value = false; window.location.hash = `#${path}`; }
const bridge = inject(BridgeKey)!;
defineEmits<{ history: []; migration: [] }>();
const hello = useQuery('settings.hello', () => bridge.call('app.hello', { clientProtocol: 1 }).then(unwrap), { visible: () => section.value === 'about' });
</script>
<template>
  <div class="settings-page"><h1>设置</h1>
    <SectionNav :model-value="section" label="设置分类" :items="sections" @update:model-value="section = $event as PreferenceValues['settingsSection']" />
    <InlineAlert v-if="saved.error.value" severity="danger" retry @retry="saved.retry">{{ saved.error.value }}</InlineAlert>
    <Display v-if="section === 'display'" :initial="saved.snapshot.value" />
    <Sources v-else-if="section === 'sources'" :mode="saved.snapshot.value.preferences.timezone" />
    <Background v-else-if="section === 'background'" :initial="saved.snapshot.value" />
    <template v-else-if="section === 'data'"><Data :mode="saved.snapshot.value.preferences.timezone" /><HistoryAndMigration @history="$emit('history')" @migration="$emit('migration')" /></template>
    <template v-else>
      <section class="settings-card"><h2>使用指南</h2><p class="text-muted">从模型池、价格组与倍率版本开始，再配置供应商与钱包。</p><div class="settings-actions"><Button @click="guideOpen = true">查看使用指南</Button></div></section>
      <InlineAlert v-if="hello.error.value" severity="danger" retry @retry="hello.refresh">{{ hello.error.value.message }}</InlineAlert>
      <About v-if="hello.data.value" :version="hello.data.value.appVersion" :data-dir="hello.data.value.dataDir" />
    </template>
    <SetupGuideDialog v-if="guideOpen" @close="guideOpen = false" @navigate="guideNavigate" />
  </div>
</template>
<style>.settings-page { display: grid; gap: 20px; min-width: 0; }.settings-card { display: grid; gap: 16px; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); min-width: 0; }.settings-card h2 { font-size: 16px; }.settings-fields { display: grid; gap: 16px; }.settings-fields label:not(.checkbox-field) { display: flex; align-items: center; flex-wrap: wrap; gap: 16px; }.settings-fields select { min-width: 180px; max-width: 100%; }.checkbox-field, .settings-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }.settings-card .path { overflow-wrap: anywhere; }</style>
