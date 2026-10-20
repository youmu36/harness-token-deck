import type { CommandData, RequestMap } from '../../app/ports';
import type { DeepReadonly } from 'vue';
export type Catalog = DeepReadonly<CommandData<'pricing.catalog'>>;
export type Group = Catalog['groups'][number];
export type Version = Group['versions'][number];
export type ModelRate = RequestMap['pricing.saveVersionPrices']['models'][number];
export type Rule = NonNullable<ModelRate['billingRule']>;
export type Pool = DeepReadonly<CommandData<'models.pool'>>;
export type DraftRow = { key: string; model: ModelRate; multiplier: string };
export function rate(value: string | null | undefined) { if (!value) return null; return value.replace(/^0+(?=\d)/, '').replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, ''); }
export function defaultRule(mode: Rule['mode']): Rule { return { mode, rates: { input: null, cache: null, output: null }, weekdays: [1, 2, 3, 4, 5], hours: [9, 10, 11, 12, 13, 14, 15, 16, 17], offsetMinutes: -new Date().getTimezoneOffset(), thresholdK: 100 }; }
