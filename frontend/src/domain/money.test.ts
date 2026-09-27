import { describe, expect, it } from 'vitest';

import { formatPoysha, formatTaka, formatTakaSymbol, speakTaka } from './money';

describe('money (integer poysha)', () => {
  it('formats the guideline figures exactly', () => {
    expect(formatTaka(5250)).toBe('52.50');
    expect(formatTaka(3000)).toBe('30.00');
    expect(formatTaka(-2250)).toBe('-22.50');
    expect(formatTaka(750000)).toBe('7,500.00');
    expect(formatTaka(5)).toBe('0.05');
    expect(formatPoysha(5250)).toBe('5,250 poysha');
    expect(formatTakaSymbol(-2250)).toBe('−৳22.50');
    expect(speakTaka(5250)).toBe('52 taka 50 poysha');
  });

  it('refuses floating-point money', () => {
    expect(() => formatTaka(52.5)).toThrow(TypeError);
    expect(() => formatPoysha(0.1 + 0.2)).toThrow(TypeError);
  });
});
