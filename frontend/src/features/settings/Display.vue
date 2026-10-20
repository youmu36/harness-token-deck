<script setup lang="ts">
import { ref, watch } from 'vue';
import type { CommandData, CommandPayload } from '../../app/ports';
import { useShellPreferences } from '../../app/useShellPreferences';
import { harnessNames, type Harness } from '../harness/types';
import InlineAlert from '../../ui/InlineAlert.vue';
const props = defineProps<{ initial: CommandData<'preferences.get'> }>();
type PreferenceValues = CommandData<'preferences.get'>['preferences'];
type PreferencePatch = CommandPayload<'preferences.update'>['patch'];
const preferences = useShellPreferences(props.initial);
const values = ref({ ...props.initial.preferences }); const pending = ref(false);
watch(preferences.snapshot, value => { if (!pending.value) values.value = { ...value.preferences }; });
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
async function save(field: 'theme' | 'timezone' | 'showScanCountdown' | 'hiddenHarnesses', value: PreferenceValues[typeof field]) {
  values.value = { ...values.value, [field]: value }; pending.value = true;
  try { await preferences.save({ [field]: value } as PreferencePatch); } catch { /* Section error, then rollback. */ }
  finally { pending.value = false; values.value = { ...preferences.snapshot.value.preferences }; }
}
function toggleHarness(harness: Harness, visible: boolean) {
  const hidden = new Set(values.value.hiddenHarnesses ?? []);
  if (visible) hidden.delete(harness); else hidden.add(harness);
  void save('hiddenHarnesses', [...hidden]);
}
</script>
<template>
  <section class="settings-card" aria-label="显示">
    <h2>显示</h2>
    <div class="settings-fields">
      <label>主题<select aria-label="显示主题" :value="values.theme" :disabled="pending" @change="save('theme', ($event.target as HTMLSelectElement).value as PreferenceValues['theme'])"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label>
      <label>统计时区<select aria-label="统计时区" :value="values.timezone" :disabled="pending" @change="save('timezone', ($event.target as HTMLSelectElement).value as PreferenceValues['timezone'])"><option value="system">系统 · {{ timezone }}</option><option value="UTC">UTC</option></select></label>
      <label class="checkbox-field"><input type="checkbox" aria-label="显示扫描倒计时" :checked="values.showScanCountdown" :disabled="pending" @change="save('showScanCountdown', ($event.target as HTMLInputElement).checked)" />显示扫描倒计时</label>
      <fieldset class="harness-visibility"><legend>显示 Harness 页面</legend>
        <p class="text-muted">隐藏页面后仍会采集用量，历史数据继续包含在总览中。</p>
        <label v-for="(label, harness) in harnessNames" :key="harness" class="checkbox-field"><input type="checkbox" :aria-label="`显示 ${label} 页面`" :checked="!values.hiddenHarnesses?.includes(harness)" :disabled="pending" @change="toggleHarness(harness, ($event.target as HTMLInputElement).checked)" />{{ label }}</label>
      </fieldset>
    </div>
    <InlineAlert v-if="preferences.error.value" severity="danger" retry @retry="preferences.retry">{{ preferences.error.value }}</InlineAlert>
  </section>
</template>
<style scoped>.harness-visibility { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; padding: 18px; border: 1px solid var(--border); border-radius: var(--radius); }.harness-visibility legend { padding-inline: 6px; }.harness-visibility p { grid-column: 1 / -1; font-size: 13px; }@container page-content (max-width: 480px) { .harness-visibility { grid-template-columns: 1fr; } }</style>
