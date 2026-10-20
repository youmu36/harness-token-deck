import type { CommandData } from '../../app/ports';
import type { DeepReadonly } from 'vue';

export type UsagePage = 'overview' | 'claude' | 'codex' | 'zcode' | 'dsh';
export type UsageHarness = 'all' | 'claude' | 'codex' | 'zcode' | 'dsh';
export type UsageFilter = NonNullable<CommandData<'preferences.get'>['preferences']['filters']['codex']>;
export type RangePreset = UsageFilter['range'];
export type UsageRange = { start: string; end: string; offsetMinutes: number; crossesDst: boolean; startNow?: boolean; endNow?: boolean };
export type Summary = DeepReadonly<CommandData<'stats.summary'>>;
export type CollectionSnapshot = DeepReadonly<CommandData<'collection.status'>>;
export type Metrics = Summary['metrics'];
export type ModelRow = Summary['models'][number];

export const defaultFilter: UsageFilter = {
  range: 'today', harness: 'all', start: null, end: null, grain: 'hour', metric: 'tokens', monitorMode: 'minute', modelKey: null, startNow: false, endNow: false,
};
