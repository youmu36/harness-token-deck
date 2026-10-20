import { inject, onScopeDispose } from 'vue';
import { BridgeKey, type Bridge, type CommandData } from './ports';
import { createPreferenceState } from './preferenceState';

// Mounted pages share one revision, event subscription and ordered write queue.
const shared = new WeakMap<Bridge, { state: ReturnType<typeof createPreferenceState>; users: number }>();
export function useShellPreferences(initial: CommandData<'preferences.get'>) {
  const bridge = inject(BridgeKey);
  if (!bridge) throw new Error('Shell requires an application bridge');
  let entry = shared.get(bridge);
  if (!entry) { entry = { state: createPreferenceState(bridge, initial), users: 0 }; shared.set(bridge, entry); }
  const current = entry;
  current.state.accept(initial); current.users++;
  onScopeDispose(() => { if (--current.users === 0) { current.state.dispose(); shared.delete(bridge); } });
  return current.state;
}
