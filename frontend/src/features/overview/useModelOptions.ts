import { computed, inject, onScopeDispose, ref, toValue, watch, type MaybeRefOrGetter } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';

export function useModelOptions(visible: MaybeRefOrGetter<boolean>) {
  const bridge = inject(BridgeKey)!;
  const search = ref(''), settled = ref(''), offset = ref(0);
  let timer: ReturnType<typeof setTimeout> | undefined;
  watch(search, value => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { settled.value = value; offset.value = 0; }, 250);
  });
  onScopeDispose(() => { if (timer) clearTimeout(timer); });
  const query = useQuery('analysis.modelOptions', async () => unwrap(await bridge.call('stats.exploreOptions', {
    modelSearch: settled.value, modelOffset: offset.value, modelLimit: 100, includeCatalog: false,
  })), { deps: [settled, offset], visible: () => toValue(visible), coalesce: true,
    events: ['usage.changed', 'models.changed'], eventIntervalMs: 60000 });
  return { search, offset, query, rows: computed(() => query.data.value?.models ?? []),
    total: computed(() => query.data.value?.totalModels ?? 0), next: computed(() => query.data.value?.modelNextOffset ?? null) };
}
