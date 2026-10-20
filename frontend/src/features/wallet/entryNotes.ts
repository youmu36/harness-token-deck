import type { CommandData } from '../../app/ports';
import { formatMoney } from '../../shared/money';
type Entry = CommandData<'wallet.entries'>['entries'][number];
const translated: Record<string, string> = {
  late_offset: '已校准消费的迟到抵消', migration: '旧版数据迁移修正', recalc: '历史费用重算修正', initial_balance: '期初余额', top_up: '手动充值',
  original_spending: '原始估算消费；校准与重算另列，不覆盖原额',
  original_spending_corrected: '该日估算已发生校准或修正，原消费金额保留；请查看独立调整流水',
  legacy_compacted: '升级前已压缩的历史净额；原始消费与修正无法拆分，按余额影响保留',
};
export function entryNote(entry: Entry): string {
  if (entry.note === 'calibration' && entry.calibration) return `余额校准：${formatMoney(entry.calibration.observedBalancePico, 'full6')} → ${formatMoney(entry.calibration.targetPico, 'full6')}；原消费凭证保留`;
  return translated[entry.note] ?? (entry.note || (entry.kind === 'top_up' ? '手动充值' : '余额调整'));
}
export function amountExplanation(_entry?: Entry): string {
  return '每日消费为原始计费金额，非负，表示扣款。充值表示增加资金。调整与校准单独列出：正数增加余额，负数减少余额。消费错误后的返还不会改写原消费，也不会变成负数消费。';
}
export function noteExplanation(_entry?: Entry): string {
  return '备注说明资金来源、校准前后的余额或关联消费凭证。发生校准时保留原消费，另列修正金额；同一笔修正只计算一次。升级前已压缩且无法拆分的历史会明确标注。';
}
