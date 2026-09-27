import type { VerifiedFareStructure } from '@/types/mobility';
import { formatTaka } from './money';

/**
 * Guideline fare model:  Fare = (Base + Distance) × (1 − PoolDiscount), per seat, in integer poysha.
 *
 * Tariff values are chosen so the guideline's hand-check holds on the real Banani → Gulshan 1 distance
 * (2.0 km in the matching service): 3,000 + 4,500 = 7,500; 30 % off = 2,250; net 5,250 poysha (৳52.50).
 * Rounding is always floor on integers, the same rule the fare service uses.
 */
export interface FareTariff {
  readonly basePoysha: number;
  readonly perKmPoysha: number;
  readonly poolDiscountPct: number;
}

export const DEMO_TARIFF: FareTariff = { basePoysha: 3000, perKmPoysha: 2250, poolDiscountPct: 30 };

export interface FareBreakdown {
  readonly distanceM: number;
  readonly seats: number;
  readonly pooled: boolean;
  readonly basePoysha: number;
  readonly distanceChargePoysha: number;
  readonly subtotalPoysha: number;
  readonly discountPct: number;
  readonly discountPoysha: number;
  readonly netPoysha: number;
}

export interface FareInput {
  readonly distanceM: number;
  readonly seats?: number;
  readonly pooled: boolean;
}

export function computeFare({ distanceM, seats = 1, pooled }: FareInput, tariff: FareTariff = DEMO_TARIFF): FareBreakdown {
  if (!Number.isInteger(distanceM) || distanceM < 0) throw new RangeError('distanceM must be a whole, non-negative number');
  if (!Number.isInteger(seats) || seats < 1) throw new RangeError('seats must be a positive integer');
  const basePoysha = tariff.basePoysha * seats;
  const distanceChargePoysha = Math.floor((distanceM * tariff.perKmPoysha) / 1000) * seats;
  const subtotalPoysha = basePoysha + distanceChargePoysha;
  const discountPct = pooled ? tariff.poolDiscountPct : 0;
  const discountPoysha = Math.floor((subtotalPoysha * discountPct) / 100);
  return {
    distanceM,
    seats,
    pooled,
    basePoysha,
    distanceChargePoysha,
    subtotalPoysha,
    discountPct,
    discountPoysha,
    netPoysha: subtotalPoysha - discountPoysha,
  };
}

export function toVerifiedFare(b: FareBreakdown): VerifiedFareStructure {
  return {
    baseFarePoysha: b.basePoysha,
    distanceRatePoysha: b.distanceChargePoysha,
    discountPercentage: b.discountPct,
    calculatedNetPoysha: b.netPoysha,
    formattedTakaString: formatTaka(b.netPoysha),
  };
}
