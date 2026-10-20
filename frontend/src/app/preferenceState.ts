import { ref, shallowRef } from 'vue';
import type { Bridge, CommandData } from './ports';
import { asAppError, unwrap } from './errors';
import { useCommand } from '../shared/command';
import { canonicalPayload } from '../shared/payload';
import type { PreferencePatch } from '../infra/generated/contracts';

export function createPreferenceState(bridge: Bridge, initial: CommandData<'preferences.get'>) {
  const snapshot = shallowRef(initial);
  const error = ref('');
  const command = useCommand('preferences.update', { bridge });
  let queue = Promise.resolve();
  let queued: { patch: PreferencePatch; promise: Promise<void> } | undefined;
  let retry: (() => Promise<void>) | undefined;
  let refreshing: Promise<void> | undefined;
  let dirty = false;
  function accept(next: CommandData<'preferences.get'>) {
    if (next.revision >= snapshot.value.revision) snapshot.value = next;
  }
  function refresh() {
    if (!refreshing) refreshing = bridge.call('preferences.get', {}).then(unwrap).then(accept).finally(() => { refreshing = undefined; });
    return refreshing;
  }
  const unsubscribe = bridge.events.subscribe('preferences.changed', event => {
    if (event.revision <= snapshot.value.revision) return;
    dirty = true;
    if (bridge.events.isVisible()) { dirty = false; void refresh().catch(() => {}); }
  });
  const visibility = bridge.events.onVisibility(visible => {
    if (visible && dirty) { dirty = false; void refresh().catch(() => {}); }
  });
  function persist(patch: PreferencePatch): Promise<void> {
    const operation = async () => {
      error.value = '';
      try {
        for (let attempt = 0; attempt < 3; attempt++) {
          if (Object.entries(patch).every(([key, value]) => canonicalPayload(snapshot.value.preferences[key as keyof typeof snapshot.value.preferences]) === canonicalPayload(value))) { retry = undefined; return; }
          try {
            accept(await command.run({ patch, expectedRevision: snapshot.value.revision }));
            retry = undefined; return;
          } catch (failure) {
            if (asAppError(failure).code !== 'PREFERENCES_REVISION_CONFLICT' || attempt === 2) throw failure;
            await refresh();
          }
        }
      } catch (failure) {
        const fields = [...new Set(Object.keys(patch).map(key => ({ lastPage: '当前页面', lastRoute: '当前页面', guideSeen: '使用指南状态', filters: '统计筛选条件', theme: '主题', timezone: '统计时区', collapsed: '侧栏状态', historyPrompted: '历史导入提示', showScanCountdown: '扫描倒计时', hiddenHarnesses: 'Harness 页面显示', settingsSection: '设置分类', walletDefaultScope: '默认钱包' }[key] ?? key)))].join('、');
        error.value = `未能保存${fields}：${asAppError(failure).message}。点击重试可保留本次选择。`; retry = () => save(patch);
        throw failure;
      }
    };
    return operation();
  }
  function save(patch: PreferencePatch): Promise<void> {
    if (queued) {
      const filters = patch.filters ? { ...queued.patch.filters, ...patch.filters } : queued.patch.filters;
      queued.patch = { ...queued.patch, ...patch, ...(filters ? { filters } : {}) };
      return queued.promise;
    }
    const group = { patch: JSON.parse(canonicalPayload(patch)) as PreferencePatch, promise: Promise.resolve() };
    const result = queue.then(() => { if (queued === group) queued = undefined; return persist(group.patch); });
    group.promise = result; queued = group; queue = result.catch(() => {});
    return result;
  }
  async function flush() { await queue; if (retry) await retry(); await queue; if (error.value) throw new Error(error.value); }
  return { snapshot, error, save, flush, accept, retry: () => { void retry?.().catch(() => {}); }, dispose: () => { unsubscribe(); visibility(); } };
}
