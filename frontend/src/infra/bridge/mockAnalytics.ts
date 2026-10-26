import type { CommandData, RequestMap } from '../../app/ports';
import { AppError } from '../../app/errors';

type Metrics = CommandData<'stats.analytics'>['totals'];
const names = [
  { harness: 'codex', modelKey: 'gpt-6', displayName: 'gpt-6', factor: 3 },
  { harness: 'claude', modelKey: 'claude-sonnet-4-6', displayName: 'claude-sonnet-4-6', factor: 4 },
  { harness: 'dsh', modelKey: 'deepseek-v3.2', displayName: 'deepseek-v3.2', factor: 1 },
  { harness: 'zcode', modelKey: 'claude-sonnet-4.5', displayName: 'claude-sonnet-4.5', factor: 2 },
] as const;
const day = 86400000;
const shift = 8 * 3600000;
const iso = (time: number) => new Date(time).toISOString();

export function mockAnalytics(request: RequestMap['stats.analytics']): CommandData<'stats.analytics'> {
  const now = Date.now(); const local = new Date(now + shift);
  const year = request.year ?? local.getUTCFullYear();
  const period = request.period ?? 'month';
  let start: number; let end = now;
  if (period === 'range') { start = Date.parse(request.start!); end = Date.parse(request.end!); }
  else if (period === 'all') start = Date.UTC(local.getUTCFullYear() - 2, 0, 1) - shift;
  else {
    const month = period === 'month' ? local.getUTCMonth() : period === 'quarter' ? ((request.quarter ?? Math.floor(local.getUTCMonth() / 3) + 1) - 1) * 3 : 0;
    start = Date.UTC(period === 'month' ? local.getUTCFullYear() : year, month, 1) - shift;
    const months = period === 'month' ? 1 : period === 'quarter' ? 3 : period === 'years' ? ((request.endYear ?? year) - year + 1) * 12 : 12;
    end = Math.min(now, Date.UTC(period === 'month' ? local.getUTCFullYear() : year, month + months, 1) - shift);
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) throw new AppError({ code: 'RANGE_INVALID', message: '分析范围尚未开始或结束时间无效', retryable: false });
  const selected = names.filter(item => (request.harness === 'all' || request.harness == null || item.harness === request.harness) && (request.modelKey == null || item.modelKey === request.modelKey));
  const grain = period === 'range' || period === 'month' ? 'day' : (end - start) / day > 3650 ? 'year' : 'month';
  function metrics(factor: number, from: number, to: number): Metrics {
    const days = Math.max(1, Math.ceil((to - from) / day));
    const active = factor ? Math.max(1, Math.floor((to - from) / 1800000)) : 0;
    const output = active * factor * 800;
    const input = active * factor * 18000;
    return {
      recordCount: String(active), inputTokens: String(input), outputTokens: String(output),
      cacheReadTokens: String(BigInt(input) * 7n / 10n), totalTokens: String(input + output), picoUsd: String(BigInt(active * factor) * 7200000000n),
      estimatePico: String(BigInt(active * factor) * 7200000000n), correctionPico: '0', unallocatedCorrectionPico: '0', allocationIncomplete: false,
      cacheHitRate: input ? .7 : null, cacheCoverageIncomplete: false, incomplete: false, unpriced: false,
      unknownModelCount: '0', missingUsageCount: '0', activeMinutes: String(active),
      tokensPerMinute: active ? (output / active).toFixed(2) : null, tokensPerSecond: active ? (output / active / 60).toFixed(2) : null,
      coveredDays: factor ? days : 0, coverageStart: factor ? iso(from) : null, coverageEnd: factor ? iso(to - 1) : null, lastOutputAt: factor ? iso(to - 1) : null, missingCapabilities: [],
    };
  }
  const factor = selected.reduce((sum, item) => sum + item.factor, 0);
  const models = selected.map(item => ({ ...metrics(item.factor, start, end), modelKey: item.modelKey, displayName: item.displayName })).sort((a, b) => BigInt(a.picoUsd) > BigInt(b.picoUsd) ? -1 : 1);
  function combined(from: number, to: number) { const value = metrics(factor, from, to); value.recordCount = String(BigInt(value.recordCount) * BigInt(selected.length)); return value; }
  const totals = combined(start, end);
  const trend: CommandData<'stats.analytics'>['trend'] = [];
  let cursor = start;
  while (cursor < end && trend.length < 120) {
    const date = new Date(cursor + shift);
    const next = grain === 'day' ? cursor + day : Date.UTC(date.getUTCFullYear() + (grain === 'year' ? 1 : 0), grain === 'year' ? 0 : date.getUTCMonth() + 1, 1) - shift;
    const label = date.toISOString().slice(0, grain === 'day' ? 10 : grain === 'month' ? 7 : 4);
    trend.push({ bucket: label, label, ...combined(cursor, Math.min(next, end)) }); cursor = next;
  }
  const offset = request.offset ?? 0; const limit = request.limit ?? 50;
  return {
    start: iso(start), end: iso(end), businessTimezone: 'Asia/Shanghai', grain, totals, trend,
    outputReferences: selected.flatMap(item => trend.slice(-120).map(point => ({day:point.bucket.length === 10 ? point.bucket : `${point.bucket}-01`.slice(0,10),modelKey:item.modelKey,displayName:item.displayName,tokensPerSecond:(item.factor * 800 / 60).toFixed(1),samples:3}))),
    comparison: period === 'all' ? null : { start: iso(start - (end - start)), end: iso(start), totals: combined(start - (end - start), start), changes: { tokens: '0.00', output: '0.00', records: '0.00', cost: '0.00' }, includesWholeBoundaryDays: false },
    models: models.slice(offset, offset + limit), harnesses: selected.map(item => ({ harness: item.harness, ...metrics(item.factor, start, end) })),
    totalModels: models.length, nextOffset: offset + limit < models.length ? offset + limit : null,
    includesWholeBoundaryDays: false, generatedAt: iso(now), revision: 1, basis: '浏览器合成样例',
    comparisonNotice: '按业务时区比较相同日历进度；短月与闰日对齐到上期最后一日。零基期不计算增长率。',
  };
}
