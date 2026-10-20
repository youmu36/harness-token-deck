import { computed, inject } from 'vue';
import { BridgeKey, type CommandData } from '../../app/ports';
import { useShellPreferences } from '../../app/useShellPreferences';
import { unwrap } from '../../app/errors';
import { useQuery } from '../../shared/query';
export function useWallet(initialPreferences: CommandData<'preferences.get'>) {
  const bridge = inject(BridgeKey)!;
  const query = useQuery('wallet.list', async () => unwrap(await bridge.call('wallet.list', { includeArchived: true })), { events: ['wallet.changed', 'pricing.changed'], coalesce: true });
  const catalog = useQuery('wallet.pricing', async () => unwrap(await bridge.call('pricing.catalog', {})), { events: ['pricing.changed'] });
  const preferences = useShellPreferences(initialPreferences);
  return { query, catalog, preferences, mode: computed(() => preferences.snapshot.value.preferences.timezone),
    active: computed(() => query.data.value?.vendors.filter(v => v.status === 'active') ?? []),
    archived: computed(() => query.data.value?.vendors.filter(v => v.status === 'archived') ?? []) };
}
