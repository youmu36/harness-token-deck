import type { DeepReadonly } from 'vue';
import type { CommandData } from '../../app/ports';
export type Vendor = DeepReadonly<CommandData<'wallet.list'>['vendors'][number]>;
export type WalletAction = 'create' | 'topup' | 'calibrate' | 'link' | 'rename' | 'delete' | 'restore';
export function amountPico(value: string): bigint | undefined {
  if (!/^-?[0-9]{1,9}(?:\.[0-9]{1,6})?$/.test(value)) return undefined;
  const [whole, fraction = ''] = value.replace(/^-/, '').split('.');
  const absolute = BigInt(whole!) * 1_000_000_000_000n + BigInt(fraction.padEnd(12, '0'));
  return value.startsWith('-') ? -absolute : absolute;
}
