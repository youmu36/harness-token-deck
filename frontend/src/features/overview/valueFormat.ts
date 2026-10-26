/** Display a backend decimal Token/USD value in millions without floating-point rounding. */
export function millionTokens(value: string | null | undefined): string {
  if (value == null) return '—';
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) return '—';
  const fraction = match[2] ?? '', amount = BigInt(match[1]! + fraction);
  const denominator = 1000000n * 10n ** BigInt(fraction.length);
  const rounded = (amount * 100n + denominator / 2n) / denominator;
  return `${rounded / 100n}.${String(rounded % 100n).padStart(2,'0')}${amount > 0n && rounded === 0n ? '（非零）' : ''}`;
}
