import type { DeepReadonly } from 'vue';
import type { CommandData } from '../../app/ports';
import { formatMoney } from '../../shared/money';
export type RemoteSnapshot = DeepReadonly<CommandData<'remote.get'>>;
export type RemoteObservation = NonNullable<RemoteSnapshot['observation']>;
export const kindLabels = { token_remaining: '令牌剩余额度', account_remaining: '账户剩余额度', subscription_remaining: '兼容账单剩余额度', unlimited: '不限额令牌', quota_units: '原始配额（尚未换算）' };
export const statusLabels = { unconfigured: '未配置', pending: '等待刷新', ok: '正常', stale: '过期', offline: '离线', auth_failed: '认证失败', rate_limited: '限流', failed: '查询失败' };
export function formatRemoteAmount(observation: RemoteObservation): string {
  if (observation.amountPico === null) return '';
  if (observation.currency === 'USD' && !observation.currencyUncertain) return formatMoney(observation.amountPico, 'usd4');
  const currency = observation.currencyUncertain || !observation.currency ? '（币种未确认）' : observation.currency;
  return `${remoteAmount(observation.amountPico)} ${currency}`;
}
export function remoteAmount(pico: string): string {
  const value = BigInt(pico); const absolute = value < 0n ? -value : value;
  const scaled = absolute / 1_000_000n;
  return `${value < 0n && scaled ? '-' : ''}${scaled / 1_000_000n}.${String(scaled % 1_000_000n).padStart(6, '0')}`;
}
