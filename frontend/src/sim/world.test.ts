import { describe, expect, it, vi } from 'vitest';

import { PASSENGERS } from '@/domain/cast';

import { DispatchEngine } from './engine';
import { createWorld } from './scenarios';
import { occupiedSeatCount, passengerStatus, seatSlots } from './selectors';
import {
  acceptOffer,
  cancelRide,
  completeStop,
  confirmArrival,
  confirmBoarding,
  CONTENTION_GAP_MS,
  currentRide,
  declineOffer,
  driverStops,
  markNoShow,
  NO_SHOW_WAIT_MS,
  OFFER_WINDOW_MS,
  planJoin,
  reserveSeat,
  reserveSeatRace,
  setOnline,
  tick,
  type World,
} from './world';

const T0 = 1_750_000_000_000;
const shirinSeat3 = { passenger: 'Shirin', seatIndex: 3, pickup: 'Banani', dropoff: 'Gulshan 2' } as const;

function forming(): World {
  return createWorld('pool-forming', T0);
}

describe('seeded PRD story', () => {
  it('starts with Nusrat in seat 1, Rafiq in seat 2 and seat 3 open on Bullet', () => {
    const w = forming();
    const [s1, s2, s3] = seatSlots(w);
    expect(s1.passenger).toMatchObject({ name: 'Nusrat', dropoffZone: 'Mohakhali', status: 'MATCHED' });
    expect(s2.passenger).toMatchObject({ name: 'Rafiq', dropoffZone: 'Gulshan 1', status: 'MATCHED', farePoysha: 5250 });
    expect(s3.isOccupied).toBe(false);
    expect(occupiedSeatCount(w)).toBe(2);
    expect(passengerStatus(w, 'Shirin')).toBe('IDLE');
    // Rafiq joining made the trip a pool, so Nusrat's fare carries the discount too.
    expect(currentRide(w, 'Nusrat')!.fare.pooled).toBe(true);
  });

  it('lays out the driver pipeline exactly as the guideline lists it', () => {
    const stops = driverStops(forming());
    expect(stops.map((s) => `${s.kind} ${s.passenger} @ ${s.landmark}`)).toEqual([
      'PICKUP Nusrat @ Banani Road 11',
      'PICKUP Rafiq @ Banani Star Kabab',
      'DROPOFF Rafiq @ Gulshan 1 Circle',
      'DROPOFF Nusrat @ Mohakhali Flyover',
    ]);
    expect(stops[0]!.state).toBe('current');
    expect(stops[3]!.isFinal).toBe(true);
  });
});

describe('concurrency contention for the final seat', () => {
  it("Shirin loses to Nusrat's transaction 340 ms earlier and keeps her route", () => {
    const { world, result } = reserveSeatRace(forming(), shirinSeat3, T0);
    expect(result).toEqual({ ok: false, error: { code: 'SEAT_TAKEN', seatIndex: 3, claimedBy: 'Nusrat', msAgo: CONTENTION_GAP_MS } });
    // Seat 3 now shows the successful claimant.
    expect(seatSlots(world)[2].passenger?.name).toBe('Nusrat');
    expect(world.seats[3].txId).toBe('TX-201');
    expect(occupiedSeatCount(world)).toBe(3);
    // Shirin is not dropped back to an empty screen: she is searching with her pickup/drop-off intact.
    const shirin = currentRide(world, 'Shirin')!;
    expect(shirin).toMatchObject({ status: 'REQUESTED', pickupZone: 'Banani', dropoffZone: 'Gulshan 2', seats: [] });
    expect(world.contention.Shirin).toMatchObject({ seatIndex: 3, claimedBy: 'Nusrat', msAgo: 340 });
    const alert = world.notices.find((n) => n.audience === 'Shirin' && n.urgent);
    expect(alert?.text).toBe('Seat 3 was claimed by another commuter 340ms ago. Searching for the next available pooled Tesla...');
  });

  it('a genuine double-claim never double-books the seat', () => {
    let w = forming();
    const a = reserveSeat(w, shirinSeat3, T0);
    expect(a.result.ok).toBe(true);
    w = a.world;
    const b = reserveSeat(w, { passenger: 'Nusrat', seatIndex: 3, pickup: 'Banani', dropoff: 'Mohakhali' }, T0 + 5);
    expect(b.result.ok).toBe(false);
    expect(b.world.seats[3].rideId).toBe(currentRide(w, 'Shirin')!.id);
  });

  it('when Shirin is the rival, the active commuter wins and Shirin is queued', () => {
    let w = createWorld('empty-bullet', T0);
    const { world, result } = reserveSeatRace(w, { passenger: 'Rafiq', seatIndex: 1, pickup: 'Banani', dropoff: 'Gulshan 1' }, T0);
    expect(result.ok && result.value.rivalRejected).toBe('Shirin');
    w = world;
    expect(w.contention.Shirin?.claimedBy).toBe('Rafiq');
    expect(passengerStatus(w, 'Shirin')).toBe('REQUESTED');
  });

  it("a freed seat goes to the queued searcher (Rafiq cancels → Shirin joins)", () => {
    let w = reserveSeatRace(forming(), shirinSeat3, T0).world;
    w = cancelRide(w, 'Rafiq', 'changed_plans', T0 + 1000).world;
    expect(passengerStatus(w, 'Rafiq')).toBe('CANCELLED');
    expect(currentRide(w, 'Shirin')).toMatchObject({ status: 'MATCHED', seats: [2] });
    expect(occupiedSeatCount(w)).toBe(3);
  });
});

describe('pool rules', () => {
  it('only lets riders from the pool pickup zone join, within the 140 % detour cap', () => {
    const w = forming();
    expect(planJoin(w, 'Banani', 'Gulshan 2')).toMatchObject({ ok: true, maxDetourPct: 135 });
    expect(planJoin(w, 'Uttara', 'Gulshan 2').ok).toBe(false);
    expect(planJoin(w, 'Banani', 'Uttara').ok).toBe(false);
    const res = reserveSeat(w, { ...shirinSeat3, pickup: 'Mirpur' }, T0);
    expect(res.result).toMatchObject({ ok: false, error: { code: 'ROUTE_INCOMPATIBLE' } });
  });

  it('recalculates split fares when a co-rider leaves before departure', () => {
    const w = cancelRide(forming(), 'Rafiq', 'changed_plans', T0).world;
    expect(currentRide(w, 'Nusrat')!.fare).toMatchObject({ pooled: false, netPoysha: 10875 });
    expect(w.notices.some((n) => n.audience === 'Nusrat' && n.text.includes('Fare recalculated'))).toBe(true);
  });

  it('closes the pool to new riders once it departs', () => {
    let w = forming();
    const nusrat = currentRide(w, 'Nusrat')!.id;
    w = confirmArrival(w, nusrat, T0).world;
    w = confirmBoarding(w, nusrat, T0 + 1).world;
    expect(reserveSeat(w, shirinSeat3, T0 + 2).result).toMatchObject({ ok: false, error: { code: 'POOL_DEPARTED' } });
  });
});

describe('driver cockpit flow', () => {
  it('runs all four stops sequentially, settles fares and clears the manifest', () => {
    let w = forming();
    const nusrat = currentRide(w, 'Nusrat')!.id;
    const rafiq = currentRide(w, 'Rafiq')!.id;

    // Out of order is refused.
    expect(confirmArrival(w, rafiq, T0).result).toMatchObject({ ok: false, error: { code: 'NOT_CURRENT_STOP' } });

    w = confirmArrival(w, nusrat, T0).world;
    expect(passengerStatus(w, 'Nusrat')).toBe('DRIVER_ARRIVED');
    w = confirmBoarding(w, nusrat, T0 + 10).world;
    w = confirmArrival(w, rafiq, T0 + 20).world;
    w = confirmBoarding(w, rafiq, T0 + 30).world;
    expect(passengerStatus(w, 'Rafiq')).toBe('IN_TRANSIT');

    const r = completeStop(w, rafiq, T0 + 40);
    expect(r.result).toMatchObject({ ok: true, value: { passenger: 'Rafiq', netPoysha: 5250, receiptId: 'TP-4101' } });
    w = r.world;
    expect(driverStops(w).find((s) => s.state === 'current')).toMatchObject({ passenger: 'Nusrat', kind: 'DROPOFF', isFinal: true });

    w = completeStop(w, nusrat, T0 + 50).world;
    expect(passengerStatus(w, 'Nusrat')).toBe('COMPLETED');
    expect(w.pool).toBeNull();
    expect(occupiedSeatCount(w)).toBe(0);
    expect(w.ledger.map((e) => e.netPoysha)).toEqual([7613, 5250]);
    expect(w.history[0]!.earningsPoysha).toBe(12863);
    expect(w.vehicle.currentZone).toBe('Mohakhali');
  });

  it('offers the first rider with a 15 s window; expiry re-offers, decline keeps them searching', () => {
    let w = createWorld('empty-bullet', T0);
    w = reserveSeat(w, { passenger: 'Nusrat', seatIndex: 1, pickup: 'Banani', dropoff: 'Mohakhali' }, T0).world;
    expect(w.offer).toMatchObject({ expiresAt: T0 + OFFER_WINDOW_MS });
    expect(w.seats[1].rideId).not.toBeNull();

    w = tick(w, T0 + OFFER_WINDOW_MS);
    expect(w.offer).toBeNull();
    expect(w.seats[1].rideId).toBeNull();
    w = tick(w, T0 + OFFER_WINDOW_MS + 7000);
    expect(w.offer).not.toBeNull();

    const declined = declineOffer(w, T0 + 30_000).world;
    expect(passengerStatus(declined, 'Nusrat')).toBe('REQUESTED');
    expect(tick(declined, T0 + 60_000).offer).toBeNull();

    w = acceptOffer(w, T0 + 30_000).world;
    expect(passengerStatus(w, 'Nusrat')).toBe('MATCHED');
    expect(w.pool?.pickupZone).toBe('Banani');
  });

  it('cannot go offline with riders on board, and a no-show needs the full wait', () => {
    let w = forming();
    expect(setOnline(w, false, T0).result).toMatchObject({ ok: false, error: { code: 'DRIVER_HAS_LIVE_POOL' } });
    const nusrat = currentRide(w, 'Nusrat')!.id;
    w = confirmArrival(w, nusrat, T0).world;
    expect(markNoShow(w, nusrat, T0 + 1000).result).toMatchObject({ ok: false, error: { code: 'NO_SHOW_TOO_EARLY' } });
    w = markNoShow(w, nusrat, T0 + NO_SHOW_WAIT_MS).world;
    expect(currentRide(w, 'Nusrat')).toMatchObject({ status: 'CANCELLED', cancelledBy: 'DRIVER' });
  });

  it('a passenger may cancel after arrival only inside the grace window', () => {
    let w = forming();
    const nusrat = currentRide(w, 'Nusrat')!.id;
    w = confirmArrival(w, nusrat, T0).world;
    expect(cancelRide(w, 'Nusrat', 'late', T0 + NO_SHOW_WAIT_MS).result).toMatchObject({ ok: false, error: { code: 'CANCEL_WINDOW_CLOSED' } });
    expect(cancelRide(w, 'Nusrat', 'late', T0 + 5000).result.ok).toBe(true);
  });
});

describe('DispatchEngine', () => {
  it('resolves the race after exactly the contention gap and notifies subscribers', async () => {
    vi.useFakeTimers();
    try {
      let now = T0;
      const engine = new DispatchEngine({ now: () => now });
      const listener = vi.fn();
      engine.subscribe(listener);
      const pending = engine.reserveSeat(shirinSeat3, { simulateRace: true });
      await vi.advanceTimersByTimeAsync(CONTENTION_GAP_MS - 1);
      expect(listener).not.toHaveBeenCalled();
      now += CONTENTION_GAP_MS;
      await vi.advanceTimersByTimeAsync(1);
      expect(await pending).toMatchObject({ ok: false, error: { code: 'SEAT_TAKEN' } });
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('fails every third command on a flaky network without touching state', async () => {
    vi.useFakeTimers();
    try {
      const engine = new DispatchEngine({ now: () => T0, latencyMs: 10 });
      engine.setFlaky(true);
      const before = engine.getSnapshot();
      const results = [];
      for (let i = 0; i < 3; i++) {
        const p = engine.setOnline(true);
        await vi.advanceTimersByTimeAsync(10);
        results.push(await p);
      }
      expect(results.map((r) => r.ok)).toEqual([true, true, false]);
      expect(results[2]).toEqual({ ok: false, error: { code: 'NETWORK' } });
      expect(engine.getSnapshot().rides).toBe(before.rides);
    } finally {
      vi.useRealTimers();
    }
  });
});

it('seed data uses only the PRD cast', () => {
  const w = forming();
  const names = new Set(Object.values(w.rides).map((r) => r.passenger));
  for (const n of names) expect(Object.keys(PASSENGERS)).toContain(n);
});
