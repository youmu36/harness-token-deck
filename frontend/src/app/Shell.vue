<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue';
import { Activity, ChartColumn, Code2, PanelLeftClose, PanelLeftOpen, Settings, Wallet, Tag, Terminal, Cpu } from 'lucide-vue-next';
import { UsageNavigationKey } from './usageNavigation';
import { BridgeKey, NavigationGuardKey, type CommandData } from './ports';
import { pagePaths, useRouter, type Page } from './router';
import { createTheme } from './theme';
import { useShellPreferences } from './useShellPreferences';
import IconButton from '../ui/IconButton.vue';
import InlineAlert from '../ui/InlineAlert.vue';
import Toast from '../ui/Toast.vue';
import OverviewPage from '../features/overview/OverviewPage.vue';
import HarnessPage from '../features/harness/HarnessPage.vue';
import WalletPage from '../features/wallet/WalletPage.vue';
import PricingPage from '../features/pricing/PricingPage.vue';
import SettingsPage from '../features/settings/SettingsPage.vue';
import MigrationWizard from '../features/migration/MigrationWizard.vue';
import Button from '../ui/Button.vue';
import SetupGuideDialog from '../features/settings/SetupGuideDialog.vue';
import { unwrap } from './errors';
const props = defineProps<{ hello: CommandData<'app.hello'>; initialPreferences: CommandData<'preferences.get'> }>();
const bridge = inject(BridgeKey)!;
const { snapshot, error, save, flush, retry } = useShellPreferences(props.initialPreferences);
const migrationOpen = ref(false);
const migrationHint = ref(false);
const guideOpen = ref(false);
async function closeGuide(path?: string) {
  guideOpen.value = false;
  void save({ guideSeen: true, historyPrompted: true }).catch(() => {});
  if (path) window.location.hash = `#${path}`;
  try { const found = unwrap(await bridge.call('migration.detect', {})); migrationHint.value = !snapshot.value.preferences.migrationHintDismissed && found.detected && found.targetEmpty; } catch { /* Settings keeps the import entry available. */ }
}
function dismissMigrationHint() {
  migrationHint.value = false;
  void save({ migrationHintDismissed: true }).catch(() => {});
}
function openMigration() { migrationOpen.value = true; }
function dismissGuide() { void save({ guideDismissed: true }).catch(() => {}); }
let navigationGuard: ((proceed: () => void) => void) | undefined;
provide(NavigationGuardKey, guard => { navigationGuard = guard; return () => { if (navigationGuard === guard) navigationGuard = undefined; }; });
const router = useRouter(snapshot.value.preferences.lastPage, window, proceed => navigationGuard ? navigationGuard(proceed) : proceed(), snapshot.value.preferences.lastRoute);
const workspace = ref<HTMLElement>();
const main = ref<HTMLElement>(); const skipLink = ref<HTMLAnchorElement>();
const skipLinkVisible = ref(false); let tabIntent = false;
function resetSkipLink() { tabIntent = false; skipLinkVisible.value = false; }
function skipKeyDown(event: KeyboardEvent) { tabIntent = event.key === 'Tab' && !event.ctrlKey && !event.metaKey && !event.altKey; }
function skipKeyUp() { tabIntent = false; }
function skipPointer(event: PointerEvent) { tabIntent = false; if (!skipLink.value?.contains(event.target as Node)) skipLinkVisible.value = false; }
function skipVisibility() { if (document.hidden) resetSkipLink(); }
function focusSkipLink() { skipLinkVisible.value = tabIntent; }
function skipToContent() { workspace.value?.scrollTo({ top: 0, left: 0 }); main.value?.focus({ preventScroll: true }); resetSkipLink(); }
const analysisFilter = ref<NonNullable<CommandData<'preferences.get'>['preferences']['filters']['codex']>>();
const returnPage = ref<Page>();
let returnScroll = 0;
let restoringScroll = false;
let pendingReturnScroll: number | undefined;
function restoreUsageScroll() { if (pendingReturnScroll === undefined) return; const top = pendingReturnScroll; pendingReturnScroll = undefined; void nextTick(() => workspace.value?.scrollTo({ top, left: 0 })); }
provide(UsageNavigationKey, {
  analyze: filter => { returnPage.value = router.page.value; returnScroll = workspace.value?.scrollTop ?? 0; analysisFilter.value = filter; router.navigate('overview'); },
  back: () => { if (!returnPage.value) return; restoringScroll = true; pendingReturnScroll = returnScroll; router.navigate(returnPage.value); returnPage.value = undefined; analysisFilter.value = undefined;
    void nextTick(() => { workspace.value?.scrollTo({ top: returnScroll, left: 0 }); restoringScroll = false; }); },
});
watch(() => router.route.value.path, () => { if (!restoringScroll) workspace.value?.scrollTo({ top: 0, left: 0 }); }, { flush: 'post' });
const compact = ref(window.innerWidth < 1100);
const collapsed = computed(() => compact.value || snapshot.value.preferences.collapsed);
const theme = createTheme(snapshot.value.preferences.theme);
const collapsePending = ref(false);
watch(() => snapshot.value.preferences.theme, value => theme.set(value));
const allGroups = [
  { label: '用量', items: [
    { page: 'claude', label: 'Claude Code', icon: Terminal },
    { page: 'codex', label: 'Codex', icon: Code2 },
    { page: 'dsh', label: 'DSH', icon: Cpu }, { page: 'zcode', label: 'Zcode', icon: Terminal },
  ] },
  { label: '管理', items: [
    { page: 'pricing', label: '价格与模型', icon: Tag }, { page: 'wallet', label: '供应商与钱包', icon: Wallet },
    { page: 'overview', label: '数据统计与分析', icon: ChartColumn },
    { page: 'settings', label: '设置', icon: Settings },
  ] },
] as const;
const groups = computed(() => allGroups.map(group => ({ ...group, items: group.items.filter(item => !snapshot.value.preferences.hiddenHarnesses?.includes(item.page as 'claude' | 'codex' | 'zcode' | 'dsh')) })));
watch([router.page, () => snapshot.value.preferences.hiddenHarnesses], ([page, hidden]) => {
  if (hidden?.includes(page as 'claude' | 'codex' | 'zcode' | 'dsh')) router.navigate('overview');
}, { immediate: true });
const preview = bridge.mode === 'preview';
const resize = () => { compact.value = window.innerWidth < 1100; };
function savePage() { void save({ lastPage: router.page.value, lastRoute: router.route.value.path }).catch(() => {}); }
const hostWindow = window as Window & { __htuFlushPreferences?: () => Promise<void> };
const flushPreferences = async () => { savePage(); await flush(); };
hostWindow.__htuFlushPreferences = flushPreferences;
watch(router.route, savePage, { immediate: true, flush: 'sync' });
const unsubscribe = bridge.events.onVisibility(visible => { if (!visible) { resetSkipLink(); savePage(); } });
async function toggleCollapsed() {
  collapsePending.value = true;
  try { await save({ collapsed: !snapshot.value.preferences.collapsed }); } catch { /* Inline error keeps navigation usable. */ }
  finally { collapsePending.value = false; }
}
function navigate(event: MouseEvent, page: Page) {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault(); router.navigate(page);
}
onMounted(() => {
  window.addEventListener('keydown', skipKeyDown); window.addEventListener('keyup', skipKeyUp);
  window.addEventListener('pointerdown', skipPointer); window.addEventListener('blur', resetSkipLink);
  document.addEventListener('visibilitychange', skipVisibility);
  window.addEventListener('resize', resize); window.addEventListener('beforeunload', savePage); window.addEventListener('pagehide', savePage);
  void bridge.call('migration.detect', {}).then(unwrap).then(result => {
    if (result.detected && result.targetEmpty) migrationHint.value = !snapshot.value.preferences.migrationHintDismissed;
  }).catch(() => {});
});
onBeforeUnmount(() => {
  window.removeEventListener('keydown', skipKeyDown); window.removeEventListener('keyup', skipKeyUp);
  window.removeEventListener('pointerdown', skipPointer); window.removeEventListener('blur', resetSkipLink);
  document.removeEventListener('visibilitychange', skipVisibility);
  if (hostWindow.__htuFlushPreferences === flushPreferences) delete hostWindow.__htuFlushPreferences;
  unsubscribe(); theme.dispose(); window.removeEventListener('resize', resize);
  window.removeEventListener('beforeunload', savePage); window.removeEventListener('pagehide', savePage);
});
</script>

<template>
  <div class="shell" :class="{ collapsed }">
    <a ref="skipLink" class="skip-link" :class="{ 'keyboard-focus': skipLinkVisible }" href="#main-content" @focus="focusSkipLink" @blur="skipLinkVisible = false" @click.prevent="skipToContent">跳到内容</a>
    <aside class="sidebar" aria-label="侧栏">
      <div class="brand" :title="collapsed ? 'Harness Token Usage' : undefined"><span class="brand-icon"><Activity :size="25" aria-hidden="true" /></span><span v-if="!collapsed">Harness<br />Token Usage</span></div>
      <nav aria-label="主导航">
        <section v-for="group in groups" :key="group.label" :aria-label="group.label" class="nav-group">
          <h2 class="sr-only">{{ group.label }}</h2>
          <a v-for="item in group.items" :key="item.page" :href="`#${pagePaths[item.page]}`" :aria-label="item.label" :title="collapsed ? item.label : undefined"
            :aria-current="router.page.value === item.page ? 'page' : undefined" @click="navigate($event, item.page)">
            <component :is="item.icon" :size="19" aria-hidden="true" /><span v-if="!collapsed">{{ item.label }}</span>
          </a>
        </section>
      </nav>
      <div class="sidebar-bottom">
        <IconButton :label="compact ? '窗口较窄，导航已自动折叠' : collapsed ? '展开导航' : '折叠导航'" :disabled="compact || collapsePending" :aria-expanded="!collapsed" @click="toggleCollapsed">
          <PanelLeftOpen v-if="collapsed" :size="19" /><PanelLeftClose v-else :size="19" />
        </IconButton>
        <div class="environment" :title="`${preview ? '浏览器预览' : '本机账本'} · v${hello.appVersion}`">
          <span :class="{ 'sr-only': collapsed }">{{ preview ? '浏览器预览' : '本机账本' }}</span><span>v{{ hello.appVersion }}</span>
        </div>
      </div>
    </aside>
    <div ref="workspace" class="workspace">
      <div v-if="error" class="shell-error"><InlineAlert severity="danger" retry @retry="retry">{{ error }}</InlineAlert></div>
      <div v-if="!snapshot.preferences.guideDismissed" class="shell-error" role="status"><InlineAlert>欢迎使用。跟着使用指南完成配置，即可查看用量与钱包账目。<Button @click="guideOpen = true">打开使用指南</Button><Button @click="dismissGuide">不再显示</Button></InlineAlert></div>
      <div v-if="migrationHint" class="shell-error" role="status"><InlineAlert>检测到旧版数据。<Button @click="openMigration">导入旧版数据…</Button><Button aria-label="关闭旧版数据提示" @click="dismissMigrationHint">关闭提示</Button></InlineAlert></div>
      <main id="main-content" ref="main" class="content page-content" tabindex="-1">
        <OverviewPage v-if="router.page.value === 'overview'" :initial-preferences="snapshot" :analysis-filter="analysisFilter" :can-return="!!returnPage" />
        <HarnessPage v-else-if="router.page.value === 'claude' || router.page.value === 'codex' || router.page.value === 'zcode' || router.page.value === 'dsh'" :key="router.page.value" :harness="router.page.value" :initial-preferences="snapshot" @ready="restoreUsageScroll" />
        <WalletPage v-else-if="router.page.value === 'wallet'" :initial-preferences="snapshot" :detail="router.route.value.detail" />
        <PricingPage v-else-if="router.page.value === 'pricing'" :models="router.route.value.models" :unpriced="router.route.value.unpriced" :versions="router.route.value.versions" />
        <SettingsPage v-else :initial="snapshot" :background="router.route.value.background" @migration="openMigration" />
      </main>
    </div>
    <Toast />
    <SetupGuideDialog v-if="guideOpen" first-launch @close="closeGuide()" @navigate="closeGuide($event)" />
    <MigrationWizard v-if="migrationOpen" @close="migrationOpen = false" @activated="migrationHint = false" />
  </div>
</template>

<style scoped>
.shell { display: grid; grid-template-columns: var(--nav-width) minmax(0, 1fr); height: 100dvh; overflow: hidden; }
.shell.collapsed { grid-template-columns: var(--nav-width-collapsed) minmax(0, 1fr); }
.sidebar { min-height: 0; overflow-y: auto; height: 100dvh; display: flex; flex-direction: column; background: var(--surface); border-right: 1px solid var(--border); padding: 28px 18px 18px; }
.brand { display: flex; align-items: center; gap: 12px; min-height: 44px; font-size: 15px; line-height: 1.25; font-weight: 700; padding-left: 6px; }
.brand-icon { display: grid; place-items: center; flex-shrink: 0; width: 38px; height: 38px; background: var(--accent); color: var(--accent-contrast); border-radius: 9px; box-shadow: 0 0 16px var(--chart-glow); }
nav { margin-top: 44px; }
.nav-group + .nav-group { margin-top: 28px; }
nav a { display: flex; align-items: center; gap: 14px; min-height: 48px; padding: 10px 14px; margin-block: 6px; text-decoration: none; color: var(--text-muted); border-left: 3px solid transparent; border-radius: 7px; }
nav a:hover { background: var(--surface-2); }
nav a[aria-current='page'] { background: var(--accent-soft); color: var(--accent); border-left-color: var(--accent); font-weight: 600; }
nav a svg { flex-shrink: 0; }
.collapsed nav a { justify-content: center; padding-inline: 0; }
.collapsed .brand { justify-content: center; padding: 0; }
.sidebar-bottom { margin-top: auto; display: grid; gap: 10px; }
.environment { font-size: 11px; color: var(--text-muted); display: flex; justify-content: space-between; gap: 6px; padding-inline: 6px; }
.collapsed .environment { justify-content: center; padding: 0; font-size: 10px; }
.workspace { min-width: 0; min-height: 0; overflow: auto; overscroll-behavior: contain; background: var(--page-wash); }
.page-content { padding-block: 44px; container: page-content / inline-size; }
.collapsed .sidebar { padding-inline: 8px; }
.shell-error { padding: 16px 32px 0; }
.skip-link { position: fixed; top: -80px; left: 240px; z-index: 10; padding: 8px 16px; background: var(--surface); }
.skip-link.keyboard-focus:focus-visible { top: 8px; }
</style>
