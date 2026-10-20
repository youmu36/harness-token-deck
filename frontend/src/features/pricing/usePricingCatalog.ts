import { computed, inject, ref, watch } from 'vue';
import { BridgeKey } from '../../app/ports';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';

/** Catalog selection stays independent of editor drafts and layout/navigation. */
export function usePricingCatalog() {
  const bridge = inject(BridgeKey)!;
  const catalog = useQuery('pricing.catalog', () => bridge.call('pricing.catalog', {}).then(unwrap), { events: ['models.changed', 'pricing.changed'] });
  const pool = useQuery('pricing.pool', () => bridge.call('models.pool', {}).then(unwrap), { events: ['models.changed', 'pricing.changed'] });
  const wallet = useQuery('pricing.wallet', () => bridge.call('wallet.list', {}).then(unwrap), { events: ['wallet.changed'] });
  const preferences = useQuery('pricing.preferences', () => bridge.call('preferences.get', {}).then(unwrap), { events: ['preferences.changed'] });
  const groupId = ref(''); const versionId = ref('');
  const group = computed(() => catalog.data.value?.groups.find(item => item.id === groupId.value));
  const version = computed(() => group.value?.versions.find(item => item.id === versionId.value));
  const defaultVersion = () => group.value?.versions.find(item => item.current)?.id ?? group.value?.versions[0]?.id ?? '';
  watch(catalog.data, snapshot => {
    if (!snapshot) return;
    if (!snapshot.groups.some(item => item.id === groupId.value)) groupId.value = snapshot.groups[0]?.id ?? '';
    if (!group.value?.versions.some(item => item.id === versionId.value)) versionId.value = defaultVersion();
  });
  function selectGroup(id: string) { groupId.value = id; versionId.value = defaultVersion(); }
  async function saved(g: string | null, v: string | null) {
    await catalog.refresh();
    if (g && catalog.data.value?.groups.some(item => item.id === g)) selectGroup(g);
    if (v && group.value?.versions.some(item => item.id === v)) versionId.value = v;
  }
  return { catalog, pool, wallet, preferences, groupId, versionId, group, version, selectGroup, saved };
}
