import { inject, onScopeDispose, reactive, watch } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { useShellPreferences } from '../../app/useShellPreferences';
import { emptyScope } from './scope';
import { defaultFilter } from '../usage-common/types';

export type AnalysisView = NonNullable<CommandData<'preferences.get'>['preferences']['analysisView']>;
export const defaultAnalysisView = (): AnalysisView => ({
  scope: { ...emptyScope() } as AnalysisView['scope'], draft: { ...emptyScope() } as AnalysisView['draft'],
  costBasis: 'local', section: 'statistics', period: 'month', panel: 'throughput',
  year: new Date().getFullYear(), endYear: new Date().getFullYear(), quarter: Math.floor(new Date().getMonth() / 3) + 1,
  compareDimension: 'vendor', compareIds: [], focus: null, recent: { ...defaultFilter }, draftStart: '', draftEnd: '', tpsModel:null, tpsCompareIds:[],
});
const states = new WeakMap<object, Map<string, AnalysisView>>();
export function useAnalysisState(initial: CommandData<'preferences.get'>, page: 'analysisView' | 'statisticsView' = 'analysisView') {
  const bridge = inject(BridgeKey)!;
  const preferences = useShellPreferences(initial);
  let pages = states.get(bridge);
  if (!pages) { pages = new Map(); states.set(bridge, pages); }
  let state = pages.get(page);
  if (!state) {
    const saved = initial.preferences[page];
    state = reactive<AnalysisView>({ ...defaultAnalysisView(), ...JSON.parse(JSON.stringify(saved ?? {})) as Partial<AnalysisView>,
      recent: { ...defaultFilter, ...(page === 'statisticsView' ? initial.preferences.filters.overview : {}), ...saved?.recent },
    });
    if (!saved && page === 'statisticsView') state.period = 'range';
    if (state.period === 'years') state.period = 'year';
    if (state.section === 'quality') state.section = 'statistics';
    state.costBasis = 'local';
    // Old model-detail selections no longer narrow the main comparison page.
    state.focus = null;
    state.scope.vendorIds = []; state.draft.vendorIds = [];
    if (page === 'statisticsView') { state.scope.modelKeys = []; state.draft.modelKeys = []; }
    pages.set(page, state);
  }
  const view = state;
  let timer: ReturnType<typeof setTimeout> | undefined;
  function save() {
    timer = undefined;
    void preferences.save({ [page]: JSON.parse(JSON.stringify(view)) as AnalysisView }).catch(() => {});
  }
  watch(view, () => { if (timer) clearTimeout(timer); timer = setTimeout(save, 300); }, { deep: true });
  onScopeDispose(() => { if (timer) { clearTimeout(timer); save(); } });
  return { view, error: preferences.error, retry: preferences.retry };
}
