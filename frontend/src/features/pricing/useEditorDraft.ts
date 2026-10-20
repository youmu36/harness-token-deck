import { computed, ref, toRaw } from 'vue';
import { canonicalPayload } from '../../shared/payload';
import { rate, type DraftRow, type Version, type ModelRate, type Rule } from './types';
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(toRaw(value)));
export function useEditorDraft(initial: Version) {
  const rows = ref<DraftRow[]>([]); const baseline = ref<DraftRow[]>([]);
  const modelSnapshot = (row: DraftRow): ModelRate => ({ ...row.model, modelId: row.model.modelId.trim(), input: rate(row.model.input), cache: rate(row.model.cache), cacheWrite: rate(row.model.cacheWrite), output: rate(row.model.output), billingRule: row.model.billingRule ? { ...row.model.billingRule, weekdays: [...row.model.billingRule.weekdays].sort((a,b) => a-b) as Rule['weekdays'], hours: [...row.model.billingRule.hours].sort((a,b) => a-b) as Rule['hours'], rates: { input: rate(row.model.billingRule.rates.input), cache: rate(row.model.billingRule.rates.cache), cacheWrite: rate(row.model.billingRule.rates.cacheWrite), output: rate(row.model.billingRule.rates.output) } } : null });
  function reset(version: Version) { rows.value = version.models.map(model => ({ key: crypto.randomUUID(), model: clone(model) as ModelRate, multiplier: version.hints.find(h => h.modelId.toLowerCase() === model.modelId.toLowerCase())?.multiplier ?? '' })); baseline.value = clone(rows.value); }
  function accept() { baseline.value = clone(rows.value); }
  const models = computed(() => rows.value.map(modelSnapshot));
  const hints = computed(() => rows.value.filter(row => row.multiplier !== '').map(row => ({ modelId: row.model.modelId.trim(), multiplier: row.multiplier })));
  const key = (rows: DraftRow[]) => canonicalPayload(rows.map(modelSnapshot).sort((a,b) => a.modelId.toLowerCase().localeCompare(b.modelId.toLowerCase())));
  const pricingDirty = computed(() => key(rows.value) !== key(baseline.value));
  const dirtyCount = computed(() => {
    const old = new Map(baseline.value.map(row => [row.key, canonicalPayload([modelSnapshot(row), row.multiplier])]));
    let count = rows.value.filter(row => old.get(row.key) !== canonicalPayload([modelSnapshot(row), row.multiplier])).length;
    count += baseline.value.filter(row => !rows.value.some(item => item.key === row.key)).length;
    return count;
  });
  reset(initial);
  return { rows, models, hints, pricingDirty, dirtyCount, reset, accept };
}
