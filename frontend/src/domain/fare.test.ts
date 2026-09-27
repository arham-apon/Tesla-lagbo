import { describe, expect, it } from 'vitest';

import { computeFare, toVerifiedFare } from './fare';
import { roadDistanceM } from './zones';

describe('fare model — (Base + Distance) × (1 − Discount), integer poysha', () => {
  it("matches the guideline's hand check for Rafiq, Banani → Gulshan 1 (pooled)", () => {
    const fare = computeFare({ distanceM: roadDistanceM('Banani', 'Gulshan 1'), pooled: true });
    expect(fare).toMatchObject({
      basePoysha: 3000,
      distanceChargePoysha: 4500,
      subtotalPoysha: 7500,
      discountPct: 30,
      discountPoysha: 2250,
      netPoysha: 5250,
    });
    expect(toVerifiedFare(fare)).toEqual({
      baseFarePoysha: 3000,
      distanceRatePoysha: 4500,
      discountPercentage: 30,
      calculatedNetPoysha: 5250,
      formattedTakaString: '52.50',
    });
  });

  it('prices Nusrat, Banani → Mohakhali, pooled and solo, rounding down on integers', () => {
    const d = roadDistanceM('Banani', 'Mohakhali');
    expect(d).toBe(3500);
    expect(computeFare({ distanceM: d, pooled: false }).netPoysha).toBe(10875);
    // 10,875 × 30 % = 3,262.5 → floor 3,262
    expect(computeFare({ distanceM: d, pooled: true })).toMatchObject({ discountPoysha: 3262, netPoysha: 7613 });
  });

  it('scales per seat and always yields integers', () => {
    const fare = computeFare({ distanceM: 2000, seats: 2, pooled: true });
    expect(fare.netPoysha).toBe(10500);
    for (let m = 0; m < 20000; m += 137) {
      const f = computeFare({ distanceM: m, pooled: true });
      expect(Number.isInteger(f.netPoysha)).toBe(true);
      expect(f.netPoysha).toBe(f.subtotalPoysha - f.discountPoysha);
    }
  });

  it('rejects impossible inputs', () => {
    expect(() => computeFare({ distanceM: 12.5, pooled: false })).toThrow(RangeError);
    expect(() => computeFare({ distanceM: 1000, seats: 0, pooled: false })).toThrow(RangeError);
  });
});
