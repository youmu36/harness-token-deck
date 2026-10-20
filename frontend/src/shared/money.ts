export type MoneyMode = 'usd4' | 'trunc2' | 'full6';

const PICO_PER_USD = 1_000_000_000_000n;

/** Format an exact pico-USD string without converting through Number. */
export function formatMoney(pico: string | bigint, mode: MoneyMode): string {
  if (!['usd4', 'trunc2', 'full6'].includes(mode)) throw new TypeError('Unsupported money format');
  const value = typeof pico === 'bigint' ? pico : moneyToBigInt(pico);
  const digits = mode === 'usd4' ? 4 : mode === 'trunc2' ? 2 : 6;
  const quantum = 10n ** BigInt(12 - digits);
  const absolute = value < 0n ? -value : value;
  const rounded = mode === 'trunc2'
    ? absolute / quantum
    : (absolute + quantum / 2n) / quantum;
  const scale = 10n ** BigInt(digits);
  const whole = rounded / scale;
  const fraction = (rounded % scale).toString().padStart(digits, '0');
  const sign = value < 0n && rounded > 0n ? '-' : '';
  const grouped = whole.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  if (mode === 'usd4') return `${sign}$${grouped}.${fraction}`;
  const number = `${sign}${whole}.${fraction}`;
  return mode === 'full6' ? `${number} USD` : number;
}

export function moneyToBigInt(value: string): bigint {
  if (!/^(?:0|-?[1-9]\d*)$/.test(value)) throw new TypeError('pico must be a canonical integer string');
  return BigInt(value);
}

/** Wallet amounts display at most six decimals; storage remains exact pico. */
export function formatExactMoney(pico: string | bigint, signed = false): string {
  const value = typeof pico === 'bigint' ? pico : moneyToBigInt(pico);
  const rounded = ((value < 0n ? -value : value) + 500_000n) / 1_000_000n;
  const whole = (rounded / 1_000_000n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = (rounded % 1_000_000n).toString().padStart(6, '0').replace(/0+$/, '').padEnd(2, '0');
  return `${rounded && value < 0n ? '-' : rounded && signed && value > 0n ? '+' : ''}${whole}.${fraction} USD`;
}

export function walletAmountInput(pico: string, truncate = false): string {
  const value = moneyToBigInt(pico);
  return formatMoney(truncate ? value / 1_000_000n * 1_000_000n : value, 'full6').replace(/ USD$/, '');
}

export { PICO_PER_USD };

export function formatAxisCost(pico: string | bigint | null | undefined): string {
  if (pico === null || pico === undefined) return '--';
  const value = typeof pico === 'bigint' ? pico : moneyToBigInt(pico);
  const absolute = value < 0n ? -value : value;
  const digits = absolute === 0n ? 0 : absolute < PICO_PER_USD ? 2 : absolute < 10n * PICO_PER_USD ? 1 : 0;
  const quantum = 10n ** BigInt(12 - digits);
  const rounded = (absolute + quantum / 2n) / quantum;
  const scale = 10n ** BigInt(digits);
  return `${value < 0n && rounded ? '-' : ''}$${rounded / scale}${digits ? `.${String(rounded % scale).padStart(digits, '0')}` : ''}`;
}
