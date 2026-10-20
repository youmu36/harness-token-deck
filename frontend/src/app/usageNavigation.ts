import type { InjectionKey } from 'vue';
import type { CommandData } from './ports';
type Filter = NonNullable<CommandData<'preferences.get'>['preferences']['filters']['codex']>;
export const UsageNavigationKey: InjectionKey<{
  analyze: (filter: Filter) => void;
  back: () => void;
}> = Symbol('usage-navigation');
