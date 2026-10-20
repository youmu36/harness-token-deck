import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import vectors from '../../fixtures/format/money.json';
import { formatExactMoney, formatMoney, type MoneyMode } from '../src/shared/money';
import Money from '../src/ui/Money.vue';

describe('exact shared money vectors (including FB-04)', () => {
  it('wallet changes show at most six decimals without negative zero', () => {
    expect(formatExactMoney('123', true)).toBe('0.00 USD');
    expect(formatExactMoney('-1', true)).toBe('0.00 USD');
    expect(formatExactMoney('0', true)).toBe('0.00 USD');
    expect(formatExactMoney('1234567890123456789012345', true)).toBe('+1,234,567,890,123.456789 USD');
  });
  for (const vector of vectors) {
    it(vector.pico, () => {
      for (const mode of ['usd4', 'trunc2', 'full6'] as MoneyMode[]) {
        expect(formatMoney(vector.pico, mode)).toBe(vector[mode]);
        const wrapper = mount(Money, { props: { pico: vector.pico, mode } });
        expect(wrapper.text()).toBe(vector[mode]);
        expect(wrapper.classes()).toContain('money');
        wrapper.unmount();
      }
    });
  }
  it.each(['-0', '01', '+1', '1e3', ' 1', '1.1', ''])('rejects noncanonical pico %s', value => {
    expect(() => formatMoney(value, 'usd4')).toThrow(TypeError);
  });
});
