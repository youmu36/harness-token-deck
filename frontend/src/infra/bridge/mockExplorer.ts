import type { CommandData, RequestMap } from '../../app/ports';

type Metrics = CommandData<'stats.explore'>['totals'];
const models = [{ id: 'gpt-6', name: 'GPT-6' }, { id: 'deepseek-v3.2', name: 'DeepSeek V3.2' }, { id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6' }, { id: 'gpt-6-mini', name: 'GPT-6 Mini' }, { id: 'deepseek-r1', name: 'DeepSeek R1' }];
const options: CommandData<'stats.exploreOptions'> = {
  models: models.map(row => ({...row,parentId:null})), vendors: [{ id: 'example-a', name: '示例供应商 A', parentId:null }, { id: 'example-b', name: '示例供应商 B', parentId:null }],
  groups: [{ id: 'example-ga', name: '示例价格组 A', parentId:null }, { id: 'example-gb', name: '示例价格组 B', parentId:null }],
  versions: [{ id: 'example-va1', name: '0.9 倍率', parentId: 'example-ga' }, { id: 'example-va2', name: '1.0 倍率', parentId: 'example-ga' }, { id: 'example-vb1', name: '1.2 倍率', parentId: 'example-gb' }],
};
function exportMetrics(rows: { input: bigint; cache: bigint; output: bigint; cost: bigint }[]): Metrics {
  const input = rows.reduce((sum, r) => sum + r.input, 0n), cache = rows.reduce((sum, r) => sum + r.cache, 0n), output = rows.reduce((sum, r) => sum + r.output, 0n), cost = rows.reduce((sum, r) => sum + r.cost, 0n);
  const per = (value: bigint) => cost ? (Number(value) * 1e12 / Number(cost)).toFixed(1) : null;
  return { recordCount: String(rows.length), inputTokens: String(input), cacheReadTokens: String(cache), outputTokens: String(output), totalTokens: String(input + output), cacheWriteTokens: '0', newInput: String(input - cache), picoUsd: String(cost), cacheHitRate: input ? Number(cache) / Number(input) : null, incomplete: false, unpriced: false, cacheCoverageIncomplete: false, tokensPerDollar: per(input + output), eligibleCostPico: String(cost), eligibleTokens: String(input + output), perDollar: { input: per(input - cache), cache: per(cache), output: per(output), write: per(0n) }, parts: { input: String(input - cache), cache: String(cache), output: String(output), write: '0' } };
}
export function createMockExplorer() {
  const acknowledgements = new Map<string, string>();
  function explore(request: RequestMap['stats.explore']): CommandData<'stats.explore'> {
    const end = Date.parse(request.end), start = Math.max(Date.parse(request.start), end - 45 * 86400000);
    const rows: { input: bigint; cache: bigint; output: bigint; cost: bigint; day: string; modelKey: string; displayName: string; vendorId: string; groupId: string; versionId: string; harness: 'codex' | 'zcode' }[] = [];
    for (let at = Math.ceil(start / 86400000) * 86400000; at < end; at += 86400000) for (const [i, model] of models.entries()) for (let provider = 0; provider < 2; provider++) {
      const harness = i % 2 ? 'zcode' : 'codex';
      const vendorId = provider ? 'example-b' : 'example-a', groupId = provider ? 'example-gb' : 'example-ga', versionId = provider ? 'example-vb1' : Math.floor(at / 86400000) % 2 ? 'example-va1' : 'example-va2';
      if ((request.harnesses?.length && !request.harnesses.includes(harness)) || (request.modelKeys?.length && !request.modelKeys.includes(model.id)) || (request.vendorIds?.length && !request.vendorIds.includes(vendorId)) || (request.groupIds?.length && !request.groupIds.includes(groupId)) || (request.versionIds?.length && !request.versionIds.includes(versionId))) continue;
      const input = BigInt(60000 + (Math.floor(at / 86400000) % 9) * 2000 + i * 1000), cache = input * BigInt(provider ? 6 : 8) / 10n, output = 2200n + BigInt(i * 500);
      rows.push({ input, cache, output, cost: (input - cache + output * 4n) * BigInt(provider ? 1200000 : 900000), day: new Date(at).toISOString().slice(0, request.grain === 'month' ? 7 : request.grain === 'year' ? 4 : 10), modelKey: model.id, displayName: model.name, vendorId, groupId, versionId, harness });
    }
    const grouped = new Map<string, typeof rows>();
    const daily = new Map<string, typeof rows>();
    const split = new Map<string, typeof rows>();
    for (const row of rows) {
      grouped.set(row.modelKey, [...(grouped.get(row.modelKey) ?? []), row]);
      daily.set(row.day, [...(daily.get(row.day) ?? []), row]);
      const key = [row.harness, row.vendorId, row.groupId, row.versionId, row.modelKey].join(':');
      split.set(key, [...(split.get(key) ?? []), row]);
    }
    const ranked = [...grouped].sort((a, b) => Number(BigInt(exportMetrics(b[1]).picoUsd) - BigInt(exportMetrics(a[1]).picoUsd)));
    const offset = request.offset ?? 0, limit = request.limit ?? 50;
    return { start: request.start, end: request.end, businessTimezone: 'Asia/Shanghai', generatedAt: new Date().toISOString(), grain: request.grain ?? 'day', totals: exportMetrics(rows), models: ranked.slice(offset, offset + limit).map(([modelKey, list]) => ({ modelKey, displayName: list[0]!.displayName, ...exportMetrics(list) })), trend: [...daily].map(([day, list]) => ({ bucket: day, label: day, ...exportMetrics(list) })), breakdowns: [...split.values()].map(list => ({ harness: list[0]!.harness, vendorId: list[0]!.vendorId, groupId: list[0]!.groupId, versionId: list[0]!.versionId, modelKey: list[0]!.modelKey, displayName: list[0]!.displayName, ...exportMetrics(list) })), totalModels: ranked.length, nextOffset: offset + limit < ranked.length ? offset + limit : null, missingVersionRecords: '0', notices: [], revision: 1, quality: [], qualityTotal: 0, qualityUnacknowledged: 0, qualityNextOffset: null };
  }
  function output(request: RequestMap['stats.outputReferences']): CommandData<'stats.outputReferences'> {
    const today = new Date().toISOString().slice(0, 10);
    return { today, generatedAt: new Date().toISOString(), references: (request.modelKeys ?? []).flatMap(modelKey => Array.from({ length: 30 }, (_, index) => {
      const day = new Date(Date.now() - (29 - index) * 86400000).toISOString().slice(0, 10);
      return { day, modelKey, displayName: models.find(row => row.id === modelKey)?.name ?? modelKey, tokensPerSecond: (60 + models.findIndex(row => row.id === modelKey) * 18 + index % 8).toFixed(1), samples: 2 + index % 5 };
    })) };
  }
  return { options: () => options, explore, output, acknowledge: (items: RequestMap['stats.acknowledgeQuality']['items']) => { for (const item of items) acknowledgements.set(item.id, item.fingerprint); return { acknowledged: items.length }; } };
}
