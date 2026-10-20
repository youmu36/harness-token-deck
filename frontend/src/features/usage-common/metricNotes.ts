import type { Metrics, UsageHarness } from './types';

/** Precedence is deliberate: quality-specific notes take priority over general record notes. */
export function metricNotes(metrics: Metrics, harness: UsageHarness) {
  const general = metrics.recordCount === 0 ? '当前范围暂无入账用量'
    : metrics.unknownModelCount > 0 ? `${metrics.unknownModelCount} 条模型未知` : '';
  const hitRate = metrics.inputTokens === 0 ? '输入为 0，无法计算'
    : metrics.cacheHitRate === null ? '缓存尚未探测'
    : metrics.cacheCoverageIncomplete ? '仅按缓存已知记录加权'
    : harness === 'zcode' ? '按已探测缓存加权（Grok 未探测的 0 不计入）' : '';
  const cost = metrics.unpriced ? '含未定价用量' : metrics.incomplete ? '估算成本不完整' : '';
  return {
    newInput: metrics.cacheCoverageIncomplete ? '缓存覆盖不足，新增输入不可确定' : general,
    cacheRead: general, output: metrics.missingUsageCount > 0 ? `${metrics.missingUsageCount} 条用量缺失` : general,
    hitRate, totalTokens: general, cost,
  };
}
