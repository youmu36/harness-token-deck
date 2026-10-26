import type { CommandData, RequestMap } from '../../app/ports';
type Target = CommandData<'reconciliation.targets'>['targets'][number];
type Query = RequestMap['stats.explore'];
type Metrics = CommandData<'stats.explore'>['totals'];

// Synthetic calibration members use the same sample as mockReconciliation.
export function mockCosts(query: Query, targets: Target[]) {
  function costs(base: string, scope: Query = query) {
    let delta = 0n, unallocated = 0n; let incomplete = false;
    for (const target of targets.filter(row => row.state === 'active')) {
      if (target.scope.start >= scope.end || target.scope.end <= scope.start || (scope.vendorIds?.length && !scope.vendorIds.includes(target.vendorId))) continue;
      if (scope.harnesses?.length && !scope.harnesses.includes('codex')) continue;
      if (scope.sessionRefs?.length && !scope.sessionRefs.includes('0123456789abcdef')) continue;
      if (scope.groupIds?.length && !scope.groupIds.includes('standard')) continue;
      if (scope.versionIds?.length && !scope.versionIds.includes('')) continue;
      if (scope.modelKeys?.length && !scope.modelKeys.includes('gpt-6')) continue;
      const full = scope.start <= target.scope.start && scope.end >= target.scope.end;
      const allocated = target.allocations.length === 1 && target.allocations[0]!.confirmModelCoverage && target.allocations[0]!.modelKeys.includes('gpt-6') && target.allocations[0]!.targetPico === target.targetPico;
      const part = BigInt(target.deltaPico);
      if (full && (!scope.modelKeys?.length || allocated)) delta += part;
      if (!full || !allocated) { unallocated += part; incomplete = true; }
    }
    return { picoUsd: (BigInt(base) + delta).toString(), estimatePico: base, correctionPico: delta.toString(), unallocatedCorrectionPico: unallocated.toString(), allocationIncomplete: incomplete };
  }
  function metrics(data: Metrics, scope: Query = query): Metrics {
    const value = costs(data.picoUsd, scope), eligible = BigInt(data.eligibleCostPico) + BigInt(value.correctionPico);
    const unavailable = value.allocationIncomplete || eligible < 0n;
    const per = (tokens: string) => eligible > 0n ? (Number(tokens) * 1e12 / Number(eligible)).toFixed(1) : null;
    return { ...data, ...value, ratioUnavailable: unavailable, componentCostEstimated: value.correctionPico !== '0' || unavailable,
      eligibleCostPico: unavailable ? '0' : eligible.toString(), eligibleTokens: unavailable ? '0' : data.eligibleTokens,
      tokensPerDollar: unavailable ? null : per(data.eligibleTokens), perDollar: Object.fromEntries(Object.entries(data.parts).map(([key, count]) => [key, unavailable ? null : per(count)])) as Metrics['perDollar'] };
  }
  return { costs, metrics };
}

export function projectMockSummary(data: CommandData<'stats.summary'>, request: RequestMap['stats.summary'], targets: Target[], revision: number) {
  const query: Query = { start: request.start, end: request.end, harnesses: request.harness === 'all' ? [] : [request.harness], modelKeys: request.modelKey == null ? [] : [request.modelKey] };
  const projection = mockCosts(query, targets);
  return { ...data, costRevision: revision, metrics: { ...data.metrics, ...projection.costs(data.metrics.picoUsd) },
    models: data.models.map(row => ({ ...row, ...projection.costs(row.picoUsd, { ...query, modelKeys: [row.modelKey] }) })),
    harnesses: data.harnesses.map(row => ({ ...row, ...projection.costs(row.picoUsd, { ...query, harnesses: [row.harness] }) })),
    unallocatedTrendCorrectionPico: projection.costs(data.metrics.picoUsd).correctionPico };
}
