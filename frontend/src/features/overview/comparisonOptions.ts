import type { CommandData } from '../../app/ports';

type Options = CommandData<'stats.exploreOptions'>;

/** Reuse the small catalog; labels never require another analysis read. */
export function priceGroupOptions(options?: Options) {
  const vendors = new Map(options?.vendors.map(row => [row.id, row.name]));
  return (options?.groups ?? []).map(row => ({...row, name: `${row.name} · ${(options?.groupVendors?.[row.id] ?? []).map(id => vendors.get(id) ?? id).join('、') || '未关联供应商'}`}));
}

export function priceVersionOptions(options?: Options) {
  const groups = new Map(priceGroupOptions(options).map(row => [row.id, row.name]));
  return (options?.versions ?? []).map(row => ({...row, name: `${row.name} · ${groups.get(row.parentId ?? '') ?? '信息缺失'}`}));
}
