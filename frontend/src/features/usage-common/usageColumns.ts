import { formatInteger, formatPercent, formatMoney } from '../../shared/format';
import type { TableColumn } from '../../ui/table';
import type { Metrics } from './types';

export type MetricRow = Metrics & Record<string, unknown>;
export function usageColumns<T extends MetricRow>(sortable: boolean): TableColumn<T>[] {
  return [
    { key: 'newInput', label: '新增输入', numeric: true, sortable,
      format: (_, row) => row.cacheCoverageIncomplete ? '--' : formatInteger(row.newInput),
      compare: (a, b) => a.cacheCoverageIncomplete === b.cacheCoverageIncomplete ? a.newInput - b.newInput : a.cacheCoverageIncomplete ? -1 : 1 },
    { key: 'cacheReadTokens', label: '缓存命中', numeric: true, sortable, format: (_, row) => formatInteger(row.cacheReadTokens) },
    { key: 'outputTokens', label: '输出', numeric: true, sortable, format: (_, row) => formatInteger(row.outputTokens) },
    { key: 'cacheHitRate', label: '命中率', numeric: true, sortable,
      format: (_, row) => `${formatPercent(row.cacheHitRate)}${row.cacheCoverageIncomplete ? '*' : ''}` },
    { key: 'totalTokens', label: '总 token', numeric: true, sortable, format: (_, row) => formatInteger(row.totalTokens) },
    { key: 'picoUsd', label: '成本', numeric: true, sortable, format: (_, row) => formatMoney(row.picoUsd, 'usd4'),
      compare: (a, b) => BigInt(a.picoUsd) === BigInt(b.picoUsd) ? 0 : BigInt(a.picoUsd) > BigInt(b.picoUsd) ? 1 : -1 },
  ];
}
