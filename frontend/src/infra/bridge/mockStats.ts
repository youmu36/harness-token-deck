import { AppError } from '../../app/errors';
import type { CommandData, RequestMap } from '../../app/ports';
import type { MetricsData, TrendData } from '../generated/contracts';

type Grain = CommandData<'stats.summary'>['grain'];
const minute = 60_000;
const hour = 60 * minute;
const day = 24 * hour;
const steps = { minute, hour, twoHours: 2 * hour, day };
const harnesses = [
  { harness: 'claude', displayName: 'Claude Code', modelKey: 'claude-sonnet-4-6', model: 'Claude Sonnet 4.6', seed: 4 },
  { harness: 'codex', displayName: 'Codex', modelKey: 'gpt-6', model: 'GPT-6', seed: 1 },
  { harness: 'zcode', displayName: 'Zcode', modelKey: 'claude-sonnet-4.5', model: 'Claude Sonnet 4.5', seed: 2 },
  { harness: 'dsh', displayName: 'Deepseek Harness', modelKey: 'deepseek-v3.2', model: 'DeepSeek V3.2', seed: 3 },
] as const;

export function parseMockInstant(value: string): number {
  const instant = Date.parse(value);
  if (!Number.isFinite(instant) || new Date(instant).toISOString() !== value) {
    throw new AppError({ code: 'INVALID_REQUEST', message: '日期或时间无效', retryable: false });
  }
  return instant;
}

function emptyMetrics(): MetricsData {
  return {
    recordCount: 0, inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, newInput: 0,
    totalTokens: 0, cacheHitRate: null, cacheCoverageIncomplete: false, picoUsd: '0',
    incomplete: false, unpriced: false, unknownModelCount: 0, missingUsageCount: 0,
  };
}

function add(target: MetricsData, input: number, output: number, cache: number, pico: bigint) {
  target.recordCount++;
  target.inputTokens += input;
  target.outputTokens += output;
  target.cacheReadTokens += cache;
  target.newInput = target.inputTokens - target.cacheReadTokens;
  target.totalTokens = target.inputTokens + target.outputTokens;
  target.cacheHitRate = target.cacheReadTokens / target.inputTokens;
  target.picoUsd = (BigInt(target.picoUsd) + pico).toString();
}

function align(instant: number, offset: number, grain: Grain): number {
  const shift = offset * minute;
  return Math.floor((instant + shift) / steps[grain]) * steps[grain] - shift;
}

function bucket(instant: number, offset: number, grain: Grain) {
  const local = new Date(instant + offset * minute).toISOString().slice(0, 19);
  const absolute = Math.abs(offset);
  const suffix = `${offset < 0 ? '-' : '+'}${String(Math.floor(absolute / 60)).padStart(2, '0')}:${String(absolute % 60).padStart(2, '0')}`;
  return { bucket: local + suffix, label: grain === 'day' ? local.slice(0, 10) : local.slice(5, 16).replace('T', ' ') };
}

// Synthetic requests use absolute times, so all preview breakdowns reconcile.
export function generateMockSummary(request: RequestMap['stats.summary'], cost: (harness: string, at: number, pico: bigint) => bigint = (_harness, _at, pico) => pico): CommandData<'stats.summary'> {
  const start = parseMockInstant(request.start);
  const end = parseMockInstant(request.end);
  const span = end - start;
  if (span <= 0) throw new AppError({ code: 'RANGE_INVALID', message: '结束时间须晚于开始时间', field: 'end', retryable: false });
  if (span > 30 * day) throw new AppError({ code: 'RANGE_TOO_LONG', message: '自定义范围不能超过 30 天', retryable: false });
  const fallback = span <= 72 * hour ? 'hour' : 'day';
  const requested = request.grain ?? (span <= day ? 'minute' : fallback);
  const grain = requested === 'hour' || span <= ({ minute: day, hour: 30 * day, twoHours: 7 * day, day: 30 * day })[requested] ? requested : fallback;
  const offset = request.offsetMinutes;
  const metrics = emptyMetrics();
  const trend = new Map<number, TrendData>();
  const hourlyTotals = new Map<number, CommandData<'stats.summary'>['hourlyTotals'][number]>();
  const hourly: CommandData<'stats.summary'>['hourly'] = [];
  const models: CommandData<'stats.summary'>['models'] = [];
  const split: CommandData<'stats.summary'>['harnesses'] = [];
  const selected = harnesses.filter(item => (request.harness === 'all' || item.harness === request.harness) && (request.modelKey == null || item.modelKey === request.modelKey));
  const includes = (section: NonNullable<typeof request.sections>[number]) => request.sections == null || request.sections.includes(section);

  for (const item of selected) {
    const modelMetrics = emptyMetrics();
    let previousCache: number | undefined;
    const modelHours = new Map<number, CommandData<'stats.summary'>['hourly'][number]>();
    for (let at = Math.ceil(start / (30 * minute)) * 30 * minute; at < end; at += 30 * minute) {
      const sequence = Math.abs(Math.floor(at / (30 * minute))) + item.seed;
      const input = 20_000 + (sequence % 9) * 5_000;
      const output = 1_000 + (sequence % 5) * 300;
      const cache = sequence % 11 === 0 ? 0 : Math.floor(input * 0.7);
      const pico = cost(item.harness, at, BigInt(input - cache + output * 3) * 1_000_000n);
      add(metrics, input, output, cache, pico);
      add(modelMetrics, input, output, cache, pico);
      const pointAt = align(at, offset, grain);
      const point = trend.get(pointAt) ?? {
        ...bucket(pointAt, offset, grain), inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
        cacheHitRate: null, cacheBust: false, cacheLoss: false, contextCompacted: false, picoUsd: '0', recordCount: 0,
      };
      point.inputTokens += input;
      point.outputTokens += output;
      point.cacheReadTokens += cache;
      point.recordCount++;
      point.cacheHitRate = point.cacheReadTokens / point.inputTokens;
      point.picoUsd = (BigInt(point.picoUsd) + pico).toString();
      point.cacheBust ||= cache === 0;
      point.cacheLoss ||= previousCache !== undefined && previousCache - cache >= 40_000;
      previousCache = cache;
      trend.set(pointAt, point);
      const hourAt = align(at, offset, 'hour');
      const modelHour = modelHours.get(hourAt) ?? {
        ...bucket(hourAt, offset, 'hour'), modelKey: item.modelKey, displayName: item.model, metrics: emptyMetrics(),
      };
      add(modelHour.metrics, input, output, cache, pico);
      modelHours.set(hourAt, modelHour);
      const totalHour = hourlyTotals.get(hourAt) ?? {
        ...bucket(hourAt, offset, 'hour'), displayName: '全部模型', metrics: emptyMetrics(),
      };
      add(totalHour.metrics, input, output, cache, pico);
      hourlyTotals.set(hourAt, totalHour);
    }
    if (modelMetrics.recordCount) {
      models.push({ ...modelMetrics, modelKey: item.modelKey, displayName: item.model });
      split.push({ ...modelMetrics, harness: item.harness, displayName: item.displayName });
      hourly.push(...modelHours.values());
    }
  }
  const points: TrendData[] = [];
  if (metrics.recordCount) for (let at = align(start, offset, grain); at < end; at += steps[grain]) {
    points.push(trend.get(at) ?? {
      ...bucket(at, offset, grain), inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
      cacheHitRate: null, cacheBust: false, cacheLoss: false, contextCompacted: false, picoUsd: '0', recordCount: 0,
    });
  }
  models.sort((a, b) => BigInt(a.picoUsd) === BigInt(b.picoUsd)
    ? a.displayName.localeCompare(b.displayName) : BigInt(a.picoUsd) > BigInt(b.picoUsd) ? -1 : 1);
  hourly.sort((a, b) => a.bucket.localeCompare(b.bucket) || a.modelKey.localeCompare(b.modelKey));
  return {
    grain, metrics, trend: includes('trend') ? points : [], models: includes('models') ? models : [], hourly: includes('hourly') ? hourly : [],
    hourlyTotals: !includes('hourlyTotals') ? [] : [...hourlyTotals.entries()].sort(([a], [b]) => a - b).map(([, value]) => value),
    harnesses: request.harness === 'all' && includes('harnesses') ? split : [], revision: 1,
    start: request.start, end: request.end, offsetMinutes: offset, historyNotice: null,
  };
}

export function generateMockMonitor(request: RequestMap['stats.cacheMonitor'], now = Date.now()): CommandData<'stats.cacheMonitor'> {
  const end = Math.floor(now / minute) * minute;
  const start = request.mode === 'minute' ? end - hour : align(end - 1, request.offsetMinutes, 'hour') - 23 * hour;
  const summary = generateMockSummary({ harness: request.harness, start: new Date(start).toISOString(), end: new Date(end).toISOString(), offsetMinutes: request.offsetMinutes, grain: request.mode });
  return { start: summary.start, end: summary.end, offsetMinutes: request.offsetMinutes, grain: request.mode,
    outputSpeeds: summary.models.filter(model => model.outputTokens > 0).map(model => ({modelKey:model.modelKey,displayName:model.displayName,tokensPerSecond:(model.outputTokens / Math.max(1, summary.trend.filter(point => point.outputTokens > 0).length) / 60).toFixed(1),outputTokens:String(model.outputTokens),activeMinutes:summary.trend.filter(point => point.outputTokens > 0).length,lastOutputAt:summary.end,frozen:false,basis:'minute_estimate'})),
    sessionHitRates: summary.metrics.recordCount ? [
      {sessionKey:'0123456789abcdef',displayName:'预览模型',startedAt:new Date(end-8*minute).toISOString(),lastActivityAt:new Date(end-minute).toISOString(),cacheHitRate:summary.metrics.cacheHitRate,cacheCoverageIncomplete:summary.metrics.cacheCoverageIncomplete,frozen:false},
      {sessionKey:'123456789abcdef0',displayName:'并行会话示例',startedAt:new Date(end-5*minute).toISOString(),lastActivityAt:new Date(end-minute).toISOString(),cacheHitRate:0.82,cacheCoverageIncomplete:false,frozen:false},
      {sessionKey:'23456789abcdef01',displayName:'并行会话示例',startedAt:new Date(end-3*minute).toISOString(),lastActivityAt:new Date(end-minute).toISOString(),cacheHitRate:0.56,cacheCoverageIncomplete:false,frozen:false},
    ] : [],
    trend: summary.trend, revision: 1, nextRefreshAt: new Date(end + minute).toISOString(),
    windowKey: `${summary.start}/${summary.end}/${request.offsetMinutes}/${request.mode}` };
}
