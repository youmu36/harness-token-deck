import { computed, onScopeDispose, ref } from 'vue';
import type { PreferenceValues } from '../infra/generated/contracts';

export type Page = PreferenceValues['lastPage'];
export type GuideTarget = 'models' | 'group' | 'versions' | 'rates' | 'vendor';
export interface Route { page: Page; path: string; detail?: string; models?: boolean; unpriced?: boolean; background?: boolean; versions?: boolean; walletSection?: 'reconcile' | 'allocate' | 'missing' | 'unassigned'; guideTarget?: GuideTarget }
export const pagePaths: Record<Page, string> = {
  overview: '/overview', claude: '/harness/claude', codex: '/harness/codex', zcode: '/harness/zcode', dsh: '/harness/dsh',
  wallet: '/wallet', pricing: '/pricing', settings: '/settings',
};
export function parseRoute(hash: string): Route | undefined {
  const path = hash.replace(/^#/, '').split('?')[0] ?? '';
  const page = (Object.keys(pagePaths) as Page[]).find(name => pagePaths[name] === path);
  if (page) return { page, path };
  if (path === '/models') return { page: 'pricing', path, models: true };
  if (path === '/models/setup') return { page: 'pricing', path, models: true, guideTarget: 'models' };
  if (path === '/pricing/groups') return { page: 'pricing', path, guideTarget: 'group' };
  if (path === '/pricing/rates') return { page: 'pricing', path, guideTarget: 'rates' };
  if (path === '/pricing/versions') return { page: 'pricing', path, versions: true, guideTarget: 'versions' };
  if (path === '/wallet/setup/new') return { page: 'wallet', path, guideTarget: 'vendor' };
  if (path === '/wallet/unassigned') return { page: 'wallet', path, walletSection: 'unassigned' };
  const wallet = /^\/wallet\/([^/]+)\/(reconcile|allocate|missing)$/.exec(path);
  if (wallet) {
    try { return { page: 'wallet', path, detail: decodeURIComponent(wallet[1]!), walletSection: wallet[2] as 'reconcile' | 'allocate' | 'missing' }; } catch { return undefined; }
  }
  if (path === '/settings/background') return { page: 'settings', path, background: true };
  if (path === '/unpriced') return { page: 'pricing', path, unpriced: true };
  if (/^\/wallet\/[^/]+$/.test(path)) {
    try { return { page: 'wallet', path, detail: decodeURIComponent(path.slice(8)) }; } catch { return undefined; }
  }
  return undefined;
}
export function useRouter(initial: Page, host: Window = window, guard: (proceed: () => void) => void = proceed => proceed(), initialPath = '') {
  const fallback = host.location.hash.replace(/^#/, '').split('?')[0] ? 'codex' : initial;
  const restored = !host.location.hash.replace(/^#/, '').split('?')[0] ? parseRoute(initialPath) : undefined;
  const route = ref(parseRoute(host.location.hash) ?? restored ?? { page: fallback, path: pagePaths[fallback] });
  const query = host.location.hash.split('?')[1];
  const setHash = (path: string, replace = false) => {
    const hash = `#${path}${query ? `?${query}` : ''}`;
    if (replace) host.history.replaceState(null, '', hash); else host.location.hash = hash;
  };
  const changed = () => {
    const next = parseRoute(host.location.hash) ?? { page: 'codex', path: pagePaths.codex };
    if (next.path === route.value.path) return;
    setHash(route.value.path, true);
    guard(() => { route.value = next; setHash(next.path, true); });
  };
  setHash(route.value.path, true);
  host.addEventListener('hashchange', changed);
  onScopeDispose(() => host.removeEventListener('hashchange', changed));
  const navigate = (page: Page) => {
    if (route.value.path === pagePaths[page]) return;
    guard(() => { route.value = { page, path: pagePaths[page] }; setHash(pagePaths[page]); });
  };
  return { route, page: computed(() => route.value.page), navigate };
}
