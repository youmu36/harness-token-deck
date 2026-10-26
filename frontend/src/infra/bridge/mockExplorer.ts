import type { CommandData, RequestMap } from '../../app/ports';
import { AppError } from '../../app/errors';
import { mockCosts } from './mockCosts';

type Metrics = CommandData<'stats.explore'>['totals'];
const models = [{ id: 'gpt-6', name: 'GPT-6' }, { id: 'deepseek-v3.2', name: 'DeepSeek V3.2' }, { id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6' }, { id: 'gpt-6-mini', name: 'GPT-6 Mini' }, { id: 'deepseek-r1', name: 'DeepSeek R1' }];
const options: CommandData<'stats.exploreOptions'> = {
  groupVendors:{'example-ga':['example-a'],'example-gb':['example-b']}, selectedModels:[],
  totalModels: models.length, modelNextOffset: null,
  models: models.map(row => ({...row,parentId:null})), vendors: [{ id: 'example-a', name: '示例供应商 A', parentId:null }, { id: 'example-b', name: '示例供应商 B', parentId:null }],
  groups: [{ id: 'example-ga', name: '示例价格组 A', parentId:null }, { id: 'example-gb', name: '示例价格组 B', parentId:null }],
  versions: [{ id: 'example-va1', name: '0.9 倍率', parentId: 'example-ga' }, { id: 'example-va2', name: '1.0 倍率', parentId: 'example-ga' }, { id: 'example-vb1', name: '1.2 倍率', parentId: 'example-gb' }],
};
function exportMetrics(rows: { input: bigint; cache: bigint; output: bigint; cost: bigint }[]): Metrics {
  const input = rows.reduce((sum, r) => sum + r.input, 0n), cache = rows.reduce((sum, r) => sum + r.cache, 0n), output = rows.reduce((sum, r) => sum + r.output, 0n), cost = rows.reduce((sum, r) => sum + r.cost, 0n);
  const per = (value: bigint) => cost ? (Number(value) * 1e12 / Number(cost)).toFixed(1) : null;
  return { recordCount: String(rows.length), inputTokens: String(input), cacheReadTokens: String(cache), outputTokens: String(output), totalTokens: String(input + output), cacheWriteTokens: '0', newInput: String(input - cache), picoUsd: String(cost), cacheHitRate: input ? Number(cache) / Number(input) : null, incomplete: false, unpriced: false, cacheCoverageIncomplete: false, tokensPerDollar: per(input + output), eligibleCostPico: String(cost), eligibleTokens: String(input + output), perDollar: { input: per(input - cache), cache: per(cache), output: per(output), write: per(0n) }, parts: { input: String(input - cache), cache: String(cache), output: String(output), write: '0' }, estimatePico: String(cost), correctionPico: '0', unallocatedCorrectionPico: '0', allocationIncomplete: false, ratioUnavailable: false, componentCostEstimated: false, pendingValuationCount: '0' };
}
export function createMockExplorer(targets: () => CommandData<'reconciliation.targets'>['targets'] = () => []) {
  const acknowledgements = new Map<string, string>();
  function explore(request: RequestMap['stats.explore']): CommandData<'stats.explore'> {
    const span = Date.parse(request.end) - Date.parse(request.start), boundary = Date.now() - 62 * 86400000;
    if (request.grain === 'hour' && span > 7 * 86400000) request = {...request,grain:'day'};
    const fail = (code: string, message: string): never => { throw new AppError({ code, message, retryable: false }); };
    if (span <= 0) fail('RANGE_INVALID', '请选择有效范围');
    if (request.rangeKind === 'custom' || request.rangeKind === 'recent') {
      if (span > 30 * 86400000) fail('RANGE_TOO_LONG', '近期和自定义范围最多 30 天');
      if (Date.parse(request.start) < boundary) fail('HISTORY_CAPABILITY', '详细范围仅保留最近 62 天');
      if (Date.parse(request.end) > Date.now()) fail('RANGE_INVALID', '近期范围不能包含未来');
    }
    if ((request.sessionRefs?.length || request.activityBatchIds?.length) && Date.parse(request.start) < boundary) fail('HISTORY_CAPABILITY', '会话详细能力仅保留最近 62 天');
    const projection = mockCosts(request, targets());
    const end = Date.parse(request.end), start = Math.max(Date.parse(request.start), Date.now() - 45 * 86400000);
    const rows: { input: bigint; cache: bigint; output: bigint; cost: bigint; day: string; modelKey: string; displayName: string; vendorId: string; groupId: string; versionId: string; harness: 'codex' | 'zcode' }[] = [];
    for (let at = Math.ceil(start / 86400000) * 86400000; at < end; at += 86400000) for (const [i, model] of models.entries()) for (let provider = 0; provider < 2; provider++) {
      const harness = i % 2 ? 'zcode' : 'codex';
      const sessionRef = `mock-${harness}-${model.id}-${provider}`, activityId = `${sessionRef}-${new Date(at).toISOString().slice(0, 10)}`;
      if ((request.sessionRefs?.length && !request.sessionRefs.includes(sessionRef)) || (request.activityBatchIds?.length && !request.activityBatchIds.includes(activityId))) continue;
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
    const comparisons = (request.compareIds ?? []).map(id => ({ id, totals: projection.metrics(exportMetrics(rows.filter(row => row[request.compareDimension === 'model' ? 'modelKey' : request.compareDimension === 'group' ? 'groupId' : request.compareDimension === 'version' ? 'versionId' : 'vendorId'] === id)),{...request,[request.compareDimension === 'model' ? 'modelKeys' : request.compareDimension === 'group' ? 'groupIds' : request.compareDimension === 'version' ? 'versionIds' : 'vendorIds']:[id]}), trend: [...daily].map(([day, list]) => ({
      bucket: day, label: day,
      ...exportMetrics(list.filter(row => row[request.compareDimension === 'model' ? 'modelKey' : request.compareDimension === 'group' ? 'groupId' : request.compareDimension === 'version' ? 'versionId' : 'vendorId'] === id)),
    })) }));
    const metadata = {harnesses:[],unallocatedTrendCorrectionPico:projection.costs('0').correctionPico,plan:null,costBasis: request.costBasis ?? 'local', revision:1, coverage:[], comparisons, snapshot:{usage:1,account:1,quality:1}, accounts:[],accountTotal:0,accountNextOffset:null,remoteCostPico:'0',intervalCostPico:'0',unallocatedCostPico:'0',accountCorrections:[],accountCorrectionTotal:0,accountCorrectionNextOffset:null};
    if (request.sections?.length === 1 && request.sections[0] === 'quality') {
      const evidence: CommandData<'stats.explore'>['quality'] = ['example-a', 'example-b', ''].map((vendorId, index) => ({
        id: String(index + 1).repeat(64), fingerprint: 'a'.repeat(64), date: request.start.slice(0, 10),
        harness: 'codex', vendorId, groupId: vendorId ? `example-g${index ? 'b' : 'a'}` : '', versionId: '', modelKey: 'gpt-6', displayName: 'GPT-6',
        reasons: ['缓存覆盖不完整'], reasonCounts: { '缓存覆盖不完整': '1' }, recordCount: '1', countAccuracy: index === 2 ? 'at_least' as const : 'exact' as const,
        acknowledged: acknowledgements.get(String(index + 1).repeat(64)) === 'a'.repeat(64), costDifferencePico: null,
      })).filter(row => (!request.vendorIds?.length || request.vendorIds.includes(row.vendorId))
        && (!request.groupIds?.length || request.groupIds.includes(row.groupId)) && (!request.modelKeys?.length || request.modelKeys.includes(row.modelKey))
        && (!request.harnesses?.length || request.harnesses.includes(row.harness as 'codex'))
        && (!request.qualityReasons?.length || request.qualityReasons.includes('cache')));
      const unacknowledged = evidence.filter(row => !row.acknowledged).length;
      const filtered = evidence.filter(row => !request.qualityStatus || request.qualityStatus === 'all' || row.acknowledged === (request.qualityStatus === 'acknowledged'));
      const qualityOffset = request.qualityOffset ?? 0;
      return { ...metadata, costBasis: 'local', start: request.start, end: request.end, businessTimezone: 'Asia/Shanghai', generatedAt: new Date().toISOString(), grain: 'day',
        totals: exportMetrics([]), models: [], trend: [], breakdowns: [], totalModels: 0, nextOffset: null, missingVersionRecords: '0', notices: [],
        quality: filtered.slice(qualityOffset, qualityOffset + 100), qualityTotal: filtered.length, qualityUnacknowledged: unacknowledged,
        qualityNextOffset: qualityOffset + 100 < filtered.length ? qualityOffset + 100 : null };
    }
    if (request.costBasis === 'remote') {
      const accounts = options.vendors.filter(row => !request.vendorIds?.length || request.vendorIds.includes(row.id)).map((row,index) => ({id:`interval-${row.id}`,vendorId:row.id,start:request.start,end:request.end,method:index ? 'balance_estimate' : 'remote_usage',originalCostPico:'1250000000000',costPico:'1250000000000',bookedEstimatePico:'1200000000000',currentEstimatePico:'1250000000000',differencePico:'0',status:index ? 'estimated' as const : 'matched' as const,coverageComplete:true}));
      const intervalCostPico = String(BigInt(accounts.length)*1250000000000n);
      const corrections = accounts.length ? [{id:'example-correction',vendorId:accounts[0]!.vendorId,occurredAt:request.start,kind:'consumption_correction',costPico:'10000000000',note:'示例人工累计消费更正'}] : [];
      return {...metadata,costBasis:'remote',start:request.start,end:request.end,businessTimezone:'Asia/Shanghai',generatedAt:new Date().toISOString(),grain:request.grain ?? 'day',totals:exportMetrics([]),models:[],trend:[],breakdowns:[],totalModels:0,nextOffset:null,missingVersionRecords:'0',notices:[],quality:[],qualityTotal:0,qualityUnacknowledged:0,qualityNextOffset:null,comparisons:[],accounts,accountTotal:accounts.length,remoteCostPico:String(BigInt(intervalCostPico)+(corrections.length ? 10000000000n : 0n)),intervalCostPico,unallocatedCostPico:corrections.length ? '10000000000' : '0',accountCorrections:corrections,accountCorrectionTotal:corrections.length,coverage:[{code:'account_scope',message:'远程消费按完整账户区间展示，保留核算方法，独立更正另列。'}]};
    }
    return { ...metadata, costBasis:'local', start: request.start, end: request.end, businessTimezone: 'Asia/Shanghai', generatedAt: new Date().toISOString(), grain: request.grain ?? 'day', totals: projection.metrics(exportMetrics(rows)), harnesses: [...new Set(rows.map(row => row.harness))].map(harness => ({harness,displayName:harness,...projection.metrics(exportMetrics(rows.filter(row => row.harness === harness)),{...request,harnesses:[harness]})})), models: ranked.slice(offset, offset + limit).map(([modelKey, list]) => ({ modelKey, displayName: list[0]!.displayName, ...projection.metrics(exportMetrics(list), {...request, modelKeys:[modelKey]}) })), trend: [...daily].map(([day, list]) => ({ bucket: day, label: day, ...exportMetrics(list) })), breakdowns: [...split.values()].map(list => ({ harness: list[0]!.harness, vendorId: list[0]!.vendorId, groupId: list[0]!.groupId, versionId: list[0]!.versionId, modelKey: list[0]!.modelKey, displayName: list[0]!.displayName, ...exportMetrics(list) })), totalModels: ranked.length, nextOffset: offset + limit < ranked.length ? offset + limit : null, missingVersionRecords: '0', notices: [], revision: 1, quality: [], qualityTotal: 0, qualityUnacknowledged: 0, qualityNextOffset: null };
  }
  function output(request: RequestMap['stats.outputReferences']): CommandData<'stats.outputReferences'> {
    const today = new Date().toISOString().slice(0, 10);
    const todayReferences = (request.modelKeys ?? []).map(modelKey => ({day:today,modelKey,displayName:models.find(row => row.id === modelKey)?.name ?? modelKey,tokensPerSecond:'68.0',samples:4}));
    const end = request.end ? Date.parse(request.end) : Date.now(), start = request.start ? Date.parse(request.start) : end - 29 * 86400000;
    if ((request.vendorIds?.length || request.groupIds?.length || request.versionIds?.length) && start < Date.now() - 62 * 86400000) return {today,businessTimezone:'Asia/Shanghai',todayReferences,start:request.start ?? null,end:request.end ?? null,generatedAt:new Date().toISOString(),references:[],coverage:[{code:'tps_scope_history',message:'详细吞吐只保留最近 62 天'}]};
    return { today, businessTimezone:'Asia/Shanghai', todayReferences, start:request.start ?? null, end:request.end ?? null, coverage:[], generatedAt: new Date().toISOString(), references: (request.modelKeys ?? []).flatMap(modelKey => Array.from({ length: Math.min(2000, Math.max(0, Math.ceil((end - start) / 86400000))) }, (_, index) => {
      const day = new Date(start + index * 86400000).toISOString().slice(0, 10);
      return { day, modelKey, displayName: models.find(row => row.id === modelKey)?.name ?? modelKey, tokensPerSecond: (60 + models.findIndex(row => row.id === modelKey) * 18 + index % 8).toFixed(1), samples: 2 + index % 5 };
    })) };
  }
  return { options: (request: RequestMap['stats.exploreOptions'] = {}) => {
    const found = options.models.filter(row => row.name.toLowerCase().includes((request.modelSearch ?? '').toLowerCase()));
    const offset = request.modelOffset ?? 0, limit = request.modelLimit ?? 100;
    return {...options,...(request.includeCatalog === false ? {vendors:[],groups:[],versions:[],groupVendors:{}} : {}),selectedModels:options.models.filter(row => request.modelKeys?.includes(row.id)),models:found.slice(offset,offset+limit),totalModels:found.length,modelNextOffset:offset+limit<found.length ? offset+limit : null};
  }, explore, output, acknowledge: (items: RequestMap['stats.acknowledgeQuality']['items']) => { for (const item of items) acknowledgements.set(item.id, item.fingerprint); return { acknowledged: items.length, conflicts:0 }; } };
}
