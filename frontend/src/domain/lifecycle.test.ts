import { describe, expect, it } from 'vitest';

import {
  ARRIVAL_CANCEL_GRACE_MS,
  assertTransition,
  canTransition,
  LifecycleError,
  passengerCancelWindow,
  progressIndex,
} from './lifecycle';
import type { RideLifecycleStatus } from '@/types/mobility';

const ALL: RideLifecycleStatus[] = ['IDLE', 'REQUESTED', 'MATCHED', 'DRIVER_ARRIVED', 'IN_TRANSIT', 'COMPLETED', 'CANCELLED'];

describe('ride lifecycle state machine', () => {
  it('walks the happy path in order', () => {
    expect(canTransition('IDLE', 'REQUESTED', 'PASSENGER')).toBe(true);
    expect(canTransition('REQUESTED', 'MATCHED', 'DRIVER')).toBe(true);
    expect(canTransition('MATCHED', 'DRIVER_ARRIVED', 'DRIVER')).toBe(true);
    expect(canTransition('DRIVER_ARRIVED', 'IN_TRANSIT', 'DRIVER')).toBe(true);
    expect(canTransition('IN_TRANSIT', 'COMPLETED', 'DRIVER')).toBe(true);
  });

  it('never skips a milestone or leaves a terminal state', () => {
    expect(canTransition('MATCHED', 'IN_TRANSIT', 'DRIVER')).toBe(false);
    expect(canTransition('REQUESTED', 'COMPLETED', 'DRIVER')).toBe(false);
    for (const to of ALL) {
      for (const actor of ['PASSENGER', 'DRIVER', 'SYSTEM'] as const) {
        expect(canTransition('COMPLETED', to, actor)).toBe(false);
        expect(canTransition('CANCELLED', to, actor)).toBe(false);
      }
    }
  });

  it('lets only the driver move the ride forward', () => {
    expect(canTransition('MATCHED', 'DRIVER_ARRIVED', 'PASSENGER')).toBe(false);
    expect(canTransition('IN_TRANSIT', 'COMPLETED', 'PASSENGER')).toBe(false);
  });

  it('disables cancellation in transit and restricts it after arrival', () => {
    expect(canTransition('IN_TRANSIT', 'CANCELLED', 'PASSENGER')).toBe(false);
    expect(canTransition('IN_TRANSIT', 'CANCELLED', 'DRIVER')).toBe(false);
    expect(canTransition('DRIVER_ARRIVED', 'CANCELLED', 'PASSENGER', { now: 1000, arrivedAt: 0 })).toBe(true);
    expect(canTransition('DRIVER_ARRIVED', 'CANCELLED', 'PASSENGER', { now: ARRIVAL_CANCEL_GRACE_MS, arrivedAt: 0 })).toBe(false);
    expect(canTransition('DRIVER_ARRIVED', 'CANCELLED', 'DRIVER')).toBe(true);
    expect(() => assertTransition('IN_TRANSIT', 'CANCELLED', 'PASSENGER')).toThrow(LifecycleError);
  });

  it('describes the cancel control for every state', () => {
    expect(passengerCancelWindow('REQUESTED', 0)).toEqual({ allowed: true, remainingMs: null });
    expect(passengerCancelWindow('DRIVER_ARRIVED', 20_000, 0)).toEqual({ allowed: true, remainingMs: 40_000 });
    expect(passengerCancelWindow('DRIVER_ARRIVED', 90_000, 0).allowed).toBe(false);
    expect(passengerCancelWindow('IN_TRANSIT', 0).allowed).toBe(false);
  });

  it('maps states to the four progress milestones', () => {
    expect(ALL.map(progressIndex)).toEqual([-1, 0, 1, 1, 2, 3, -1]);
  });
});
