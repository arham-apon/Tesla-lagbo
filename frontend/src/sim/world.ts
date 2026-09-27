/**
 * The in-browser dispatch "server": a pure, deterministic model of Bullet's pool.
 *
 * Every command is `(world, input, now) → { world, result }` with no timers, randomness or I/O, so the
 * whole ride lifecycle, the seat-contention race and the fare maths are unit-testable. engine.ts adds
 * network latency, the 1 s ticker and subscriptions on top.
 *
 * Rules follow the backend (IMPLEMENTATION_PLAN A4/A5/A8): riders pool only from the same pickup zone,
 * before the pool departs, with no rider's in-vehicle distance above 140 % of their solo distance. The
 * first rider needs Jashim to accept an offer; later compatible riders auto-join.
 */
import { BULLET, PASSENGERS } from '@/domain/cast';
import { computeFare, type FareBreakdown } from '@/domain/fare';
import { ARRIVAL_CANCEL_GRACE_MS, canTransition, isLive, isTerminal } from '@/domain/lifecycle';
import type { Actor, DhakaZone, PassengerName, RideLifecycleStatus, SeatIndex } from '@/types/mobility';
import { formatTakaSymbol } from '@/domain/money';
import { roadDistanceM, zoneInfo } from '@/domain/zones';

// ---- constants ---------------------------------------------------------------------------------------------------

export const SEAT_INDICES: readonly SeatIndex[] = [1, 2, 3];
export const OFFER_WINDOW_MS = 15_000;
export const REOFFER_DELAY_MS = 6_000;
export const MAX_DETOUR_PCT = 140;
export const CONTENTION_GAP_MS = 340;
export const PICKUP_ETA_MS = 3 * 60_000;
export const NO_SHOW_WAIT_MS = ARRIVAL_CANCEL_GRACE_MS;
export const SPEED_M_PER_MIN = 250; // ~15 km/h, Dhaka peak-hour average
export const TRAFFIC_INTERVAL_MS = 20_000;
const TRAFFIC_CYCLE_SEC = [0, 90, 150, 60] as const;
const MAX_NOTICES = 40;

// ---- state -------------------------------------------------------------------------------------------------------

export interface Ride {
  readonly id: string;
  readonly txId: string;
  readonly passenger: PassengerName;
  readonly pickupZone: DhakaZone;
  readonly dropoffZone: DhakaZone;
  readonly pickupLandmark: string;
  readonly dropoffLandmark: string;
  /** Seats locked for this ride. Empty while searching without a hold. */
  readonly seats: readonly SeatIndex[];
  readonly requestedSeat: SeatIndex | null;
  readonly status: Exclude<RideLifecycleStatus, 'IDLE'>;
  readonly soloDistanceM: number;
  readonly fare: FareBreakdown;
  readonly poolTripId: string | null;
  readonly joinedExistingPool: boolean;
  readonly declinedByDriver: boolean;
  readonly offerCooldownUntil: number;
  readonly requestedAt: number;
  readonly matchedAt?: number;
  readonly arrivedAt?: number;
  readonly startedAt?: number;
  readonly completedAt?: number;
  readonly cancelledAt?: number;
  readonly cancelledBy?: Actor;
  readonly cancelReason?: string;
  readonly receiptId?: string;
  readonly rating?: number;
}

export interface SeatState {
  readonly rideId: string | null;
  readonly lockedAt: number | null;
  readonly txId: string | null;
}

export interface PoolTrip {
  readonly id: string;
  readonly pickupZone: DhakaZone;
  readonly departed: boolean;
  /** Ride ids in drop-off order (the planner's route). */
  readonly dropOrder: readonly string[];
  readonly openedAt: number;
}

export interface Offer {
  readonly rideId: string;
  readonly offeredAt: number;
  readonly expiresAt: number;
}

export interface Contention {
  readonly seatIndex: SeatIndex;
  readonly claimedBy: PassengerName;
  readonly msAgo: number;
  readonly winnerTx: string;
  readonly at: number;
}

export type NoticeTone = 'info' | 'success' | 'warning' | 'danger';

export interface Notice {
  readonly id: number;
  readonly at: number;
  readonly audience: PassengerName | 'driver';
  readonly tone: NoticeTone;
  readonly text: string;
  /** Announced with aria-live="assertive" (seat lockouts); everything else is polite. */
  readonly urgent: boolean;
}

export interface LedgerEntry {
  readonly receiptId: string;
  readonly rideId: string;
  readonly txId: string;
  readonly passenger: PassengerName;
  readonly route: string;
  readonly netPoysha: number;
  readonly pooled: boolean;
  readonly settledAt: number;
}

export interface TripRecord {
  readonly id: string;
  readonly openedAt: number;
  readonly closedAt: number;
  readonly riders: readonly {
    readonly passenger: PassengerName;
    readonly route: string;
    readonly status: 'COMPLETED' | 'CANCELLED';
    readonly netPoysha: number | null;
  }[];
  readonly earningsPoysha: number;
}

export interface World {
  readonly vehicle: { readonly online: boolean; readonly batteryPercent: number; readonly currentZone: DhakaZone };
  readonly seats: Readonly<Record<SeatIndex, SeatState>>;
  readonly rides: Readonly<Record<string, Ride>>;
  readonly current: Readonly<Record<PassengerName, string | null>>;
  readonly pool: PoolTrip | null;
  readonly offer: Offer | null;
  readonly contention: Readonly<Record<PassengerName, Contention | null>>;
  readonly notices: readonly Notice[];
  readonly ledger: readonly LedgerEntry[];
  readonly history: readonly TripRecord[];
  readonly trafficDelaySec: number;
  readonly trafficTickAt: number;
  readonly seq: { readonly tx: number; readonly receipt: number; readonly notice: number; readonly pool: number };
}

// ---- results -----------------------------------------------------------------------------------------------------

export type Rejection =
  | { readonly code: 'SEAT_TAKEN'; readonly seatIndex: SeatIndex; readonly claimedBy: PassengerName; readonly msAgo: number }
  | { readonly code: 'SAME_ZONE' }
  | { readonly code: 'ALREADY_ACTIVE' }
  | { readonly code: 'POOL_DEPARTED' }
  | { readonly code: 'ROUTE_INCOMPATIBLE'; readonly reason: string }
  | { readonly code: 'DRIVER_OFFLINE' }
  | { readonly code: 'NO_RIDE' }
  | { readonly code: 'NO_OFFER' }
  | { readonly code: 'CANCEL_WINDOW_CLOSED' }
  | { readonly code: 'INVALID_TRANSITION'; readonly message: string }
  | { readonly code: 'NOT_CURRENT_STOP' }
  | { readonly code: 'NO_SHOW_TOO_EARLY'; readonly remainingMs: number }
  | { readonly code: 'DRIVER_HAS_LIVE_POOL' }
  | { readonly code: 'NETWORK' };

export type Result<T> = { readonly ok: true; readonly value: T } | { readonly ok: false; readonly error: Rejection };
export interface Outcome<T> {
  readonly world: World;
  readonly result: Result<T>;
}

const ok = <T>(world: World, value: T): Outcome<T> => ({ world, result: { ok: true, value } });
const fail = <T>(world: World, error: Rejection): Outcome<T> => ({ world, result: { ok: false, error } });

// ---- small helpers -----------------------------------------------------------------------------------------------

const EMPTY_SEAT: SeatState = { rideId: null, lockedAt: null, txId: null };

export function emptyWorld(now: number): World {
  return {
    vehicle: { online: true, batteryPercent: 78, currentZone: 'Banani' },
    seats: { 1: EMPTY_SEAT, 2: EMPTY_SEAT, 3: EMPTY_SEAT },
    rides: {},
    current: { Nusrat: null, Rafiq: null, Shirin: null },
    pool: null,
    offer: null,
    contention: { Nusrat: null, Rafiq: null, Shirin: null },
    notices: [],
    ledger: [],
    history: [],
    trafficDelaySec: 0,
    trafficTickAt: now,
    seq: { tx: 199, receipt: 4101, notice: 1, pool: 1 },
  };
}

export const routeLabel = (r: Pick<Ride, 'pickupZone' | 'dropoffZone'>): string => `${r.pickupZone} → ${r.dropoffZone}`;

export function landmarkFor(passenger: PassengerName, zone: DhakaZone, kind: 'PICKUP' | 'DROPOFF'): string {
  const p = PASSENGERS[passenger];
  if (kind === 'PICKUP' && zone === p.pickupZone) return p.pickupLandmark;
  if (kind === 'DROPOFF' && zone === p.dropoffZone) return p.dropoffLandmark;
  return zoneInfo(zone).landmark;
}

function putRide(w: World, ride: Ride): World {
  return { ...w, rides: { ...w.rides, [ride.id]: ride } };
}

function setSeats(w: World, patch: Partial<Record<SeatIndex, SeatState>>): World {
  return { ...w, seats: { ...w.seats, ...patch } };
}

function releaseSeats(w: World, rideId: string): World {
  const patch: Partial<Record<SeatIndex, SeatState>> = {};
  for (const i of SEAT_INDICES) if (w.seats[i].rideId === rideId) patch[i] = EMPTY_SEAT;
  return setSeats(w, patch);
}

function nextTx(w: World): [World, string] {
  return [{ ...w, seq: { ...w.seq, tx: w.seq.tx + 1 } }, `TX-${w.seq.tx}`];
}

export function notify(w: World, audience: Notice['audience'], tone: NoticeTone, text: string, at: number, urgent = false): World {
  const notice: Notice = { id: w.seq.notice, at, audience, tone, text, urgent };
  return { ...w, notices: [...w.notices, notice].slice(-MAX_NOTICES), seq: { ...w.seq, notice: w.seq.notice + 1 } };
}

export function currentRide(w: World, passenger: PassengerName): Ride | null {
  const id = w.current[passenger];
  return id ? (w.rides[id] ?? null) : null;
}

/** Riders on the current pool trip that still count (not cancelled). */
export function poolMembers(w: World): Ride[] {
  if (!w.pool) return [];
  const id = w.pool.id;
  return Object.values(w.rides)
    .filter((r) => r.poolTripId === id && r.status !== 'CANCELLED')
    .sort((a, b) => (a.matchedAt ?? 0) - (b.matchedAt ?? 0) || (a.seats[0] ?? 0) - (b.seats[0] ?? 0));
}

const tripRides = (w: World): Ride[] => (w.pool ? Object.values(w.rides).filter((r) => r.poolTripId === w.pool!.id) : []);

function freeSeat(w: World, preferred: SeatIndex | null): SeatIndex | null {
  if (preferred && !w.seats[preferred].rideId) return preferred;
  return SEAT_INDICES.find((i) => !w.seats[i].rideId) ?? null;
}

// ---- pool planner (port of services/matching/app/planner.py) ------------------------------------------------------

/** Distance each rider spends in the vehicle when drop-offs happen in `order`. */
export function inVehicleDistances(pickup: DhakaZone, order: readonly { id: string; zone: DhakaZone }[]): Map<string, number> {
  const reached = new Map<string, number>();
  let total = 0;
  let prev = pickup;
  for (const stop of order) {
    total += roadDistanceM(prev, stop.zone);
    prev = stop.zone;
    reached.set(stop.id, total);
  }
  return reached;
}

export const NEW_RIDE = '__new__';

export type JoinPlan =
  | { readonly ok: true; readonly order: readonly string[]; readonly addedM: number; readonly maxDetourPct: number }
  | { readonly ok: false; readonly reason: string };

export function planJoin(w: World, pickup: DhakaZone, dropoff: DhakaZone): JoinPlan {
  const pool = w.pool;
  if (!pool) return { ok: false, reason: 'Bullet has no pool forming.' };
  if (pool.departed) return { ok: false, reason: 'Bullet has already left with its riders.' };
  if (pickup !== pool.pickupZone) {
    return { ok: false, reason: `Bullet's pool departs from ${pool.pickupZone}. Choose ${pool.pickupZone} as your pickup to share it.` };
  }
  const drops = pool.dropOrder
    .map((id) => w.rides[id])
    .filter((r): r is Ride => !!r && r.status !== 'CANCELLED')
    .map((r) => ({ id: r.id, zone: r.dropoffZone, solo: r.soloDistanceM }));
  const solo = new Map(drops.map((d) => [d.id, d.solo]));
  solo.set(NEW_RIDE, roadDistanceM(pickup, dropoff));
  const currentTotal = drops.reduce((acc, d, i) => acc + roadDistanceM(i === 0 ? pickup : drops[i - 1]!.zone, d.zone), 0);

  let best: { order: string[]; total: number; worst: number } | null = null;
  for (let i = 0; i <= drops.length; i++) {
    const order = [...drops.slice(0, i), { id: NEW_RIDE, zone: dropoff, solo: 0 }, ...drops.slice(i)];
    const reached = inVehicleDistances(pickup, order);
    let worst = 0;
    let feasible = true;
    for (const [id, inVehicle] of reached) {
      const s = solo.get(id)!;
      if (inVehicle * 100 > s * MAX_DETOUR_PCT) {
        feasible = false;
        break;
      }
      worst = Math.max(worst, Math.floor((inVehicle * 100) / s));
    }
    const total = [...reached.values()].at(-1) ?? 0;
    if (feasible && (best === null || total < best.total)) best = { order: order.map((o) => o.id), total, worst };
  }
  if (!best) {
    return { ok: false, reason: `Adding ${dropoff} would stretch a co-rider's trip past ${MAX_DETOUR_PCT}% of their direct route.` };
  }
  return { ok: true, order: best.order, addedM: best.total - currentTotal, maxDetourPct: best.worst };
}

/** In-vehicle vs direct distance for one pooled rider. */
export function rideDetour(w: World, rideId: string): { inVehicleM: number; soloM: number; pct: number } | null {
  const ride = w.rides[rideId];
  if (!ride || !w.pool || ride.poolTripId !== w.pool.id) return null;
  const order = w.pool.dropOrder
    .map((id) => w.rides[id])
    .filter((r): r is Ride => !!r && r.status !== 'CANCELLED')
    .map((r) => ({ id: r.id, zone: r.dropoffZone }));
  const inVehicleM = inVehicleDistances(w.pool.pickupZone, order).get(rideId) ?? ride.soloDistanceM;
  return { inVehicleM, soloM: ride.soloDistanceM, pct: Math.floor((inVehicleM * 100) / ride.soloDistanceM) };
}

// ---- fares -------------------------------------------------------------------------------------------------------

/** Pool discount applies while the trip has 2+ riders; recomputed on every join/leave until a ride completes. */
function recalcFares(w: World, now: number): World {
  const members = poolMembers(w);
  const pooled = members.length >= 2;
  let next = w;
  for (const r of members) {
    if (isTerminal(r.status)) continue;
    const fare = computeFare({ distanceM: r.soloDistanceM, seats: r.seats.length || 1, pooled });
    if (fare.netPoysha === r.fare.netPoysha && fare.pooled === r.fare.pooled) continue;
    next = putRide(next, { ...r, fare });
    if (r.fare.pooled !== fare.pooled) {
      next = notify(
        next,
        r.passenger,
        pooled ? 'success' : 'info',
        pooled
          ? `A co-rider joined Bullet. Pool discount applied: your fare is now ${formatTakaSymbol(fare.netPoysha)}.`
          : `Your co-rider left before departure. Fare recalculated without the pool discount: ${formatTakaSymbol(fare.netPoysha)}.`,
        now,
      );
    }
  }
  return next;
}

// ---- quotes ------------------------------------------------------------------------------------------------------

export interface Quote {
  readonly fare: FareBreakdown;
  readonly soloFare: FareBreakdown;
  /** True when this route would join Bullet's forming pool (and so gets the pool discount). */
  readonly joinsPool: boolean;
  readonly plan: JoinPlan | null;
}

/** Read-only price check for a draft route; never changes the world. */
export function quote(w: World, pickup: DhakaZone, dropoff: DhakaZone, seats = 1): Outcome<Quote> {
  if (pickup === dropoff) return fail(w, { code: 'SAME_ZONE' });
  const distanceM = roadDistanceM(pickup, dropoff);
  const plan = w.pool && !w.pool.departed ? planJoin(w, pickup, dropoff) : null;
  const joinsPool = !!plan?.ok && poolMembers(w).length > 0;
  return ok(w, {
    fare: computeFare({ distanceM, seats, pooled: joinsPool }),
    soloFare: computeFare({ distanceM, seats, pooled: false }),
    joinsPool,
    plan,
  });
}

// ---- queue, offers, pool lifecycle -------------------------------------------------------------------------------

function joinPool(w: World, ride: Ride, seat: SeatIndex, plan: Extract<JoinPlan, { ok: true }>, now: number): World {
  const pool = w.pool!;
  const joined: Ride = {
    ...ride,
    status: 'MATCHED',
    seats: [seat],
    poolTripId: pool.id,
    joinedExistingPool: true,
    matchedAt: now,
    fare: computeFare({ distanceM: ride.soloDistanceM, seats: 1, pooled: true }),
  };
  let next = putRide(w, joined);
  next = releaseSeats(next, ride.id);
  next = setSeats(next, { [seat]: { rideId: ride.id, lockedAt: now, txId: ride.txId } });
  next = {
    ...next,
    pool: { ...pool, dropOrder: plan.order.map((id) => (id === NEW_RIDE ? ride.id : id)) },
    contention: { ...next.contention, [ride.passenger]: null }, // found a seat: the lock conflict is resolved
  };
  next = notify(next, ride.passenger, 'success', `Seat ${seat} on Bullet is yours. Jashim is on the way to ${ride.pickupLandmark}.`, now);
  next = notify(next, 'driver', 'info', `${ride.passenger} joined your pool in seat ${seat} (${routeLabel(ride)}).`, now);
  return recalcFares(next, now);
}

/** Searching riders take any seat freed on a compatible pool that hasn't left yet. */
function processQueue(w: World, now: number): World {
  if (!w.pool || w.pool.departed) return w;
  let next = w;
  const waiting = Object.values(next.rides)
    .filter((r) => r.status === 'REQUESTED' && next.offer?.rideId !== r.id)
    .sort((a, b) => a.requestedAt - b.requestedAt);
  for (const ride of waiting) {
    const seat = freeSeat(next, ride.seats[0] ?? ride.requestedSeat);
    if (!seat) break;
    const plan = planJoin(next, ride.pickupZone, ride.dropoffZone);
    if (plan.ok) next = joinPool(next, ride, seat, plan, now);
    else if (ride.seats.length) next = putRide(releaseSeats(next, ride.id), { ...ride, seats: [] });
  }
  return next;
}

function nextOffer(w: World, now: number): World {
  if (!w.vehicle.online || w.offer || w.pool) return w;
  const candidate = Object.values(w.rides)
    .filter((r) => r.status === 'REQUESTED' && !r.declinedByDriver && r.offerCooldownUntil <= now)
    .sort((a, b) => a.requestedAt - b.requestedAt)[0];
  if (!candidate) return w;
  let next = releaseSeats(w, candidate.id); // its own hold counts as free
  const seat = freeSeat(next, candidate.seats[0] ?? candidate.requestedSeat);
  if (!seat) return w;
  next = setSeats(next, { [seat]: { rideId: candidate.id, lockedAt: now, txId: candidate.txId } });
  next = putRide(next, { ...candidate, seats: [seat] });
  next = { ...next, offer: { rideId: candidate.id, offeredAt: now, expiresAt: now + OFFER_WINDOW_MS } };
  return notify(next, 'driver', 'info', `New ride request: ${candidate.passenger}, ${routeLabel(candidate)}. Respond within 15 seconds.`, now);
}

function closePoolIfDone(w: World, now: number): World {
  if (!w.pool) return w;
  const rides = tripRides(w);
  if (rides.some((r) => isLive(r.status))) return w;
  const record: TripRecord = {
    id: w.pool.id,
    openedAt: w.pool.openedAt,
    closedAt: now,
    riders: rides.map((r) => ({
      passenger: r.passenger,
      route: routeLabel(r),
      status: r.status === 'COMPLETED' ? 'COMPLETED' : 'CANCELLED',
      netPoysha: r.status === 'COMPLETED' ? r.fare.netPoysha : null,
    })),
    earningsPoysha: rides.reduce((acc, r) => acc + (r.status === 'COMPLETED' ? r.fare.netPoysha : 0), 0),
  };
  let next: World = { ...w, pool: null, history: [record, ...w.history] };
  for (const i of SEAT_INDICES) {
    const holder = next.seats[i].rideId;
    if (holder && next.rides[holder]?.status !== 'REQUESTED') next = setSeats(next, { [i]: EMPTY_SEAT });
  }
  const done = rides.filter((r) => r.status === 'COMPLETED').length;
  next = notify(next, 'driver', 'success', done ? `Pool closed: ${done} rider${done > 1 ? 's' : ''} delivered. Back in the dispatch queue.` : 'Pool closed. Back in the dispatch queue.', now);
  return nextOffer(next, now);
}

/** Housekeeping that also runs after every command: seat freed → queue, pool finished → close, idle → offer. */
function settle(w: World, now: number): World {
  return nextOffer(closePoolIfDone(processQueue(recalcFares(w, now), now), now), now);
}

// ---- passenger commands ------------------------------------------------------------------------------------------

export interface SeatRequest {
  readonly passenger: PassengerName;
  readonly seatIndex: SeatIndex;
  readonly pickup: DhakaZone;
  readonly dropoff: DhakaZone;
}

export interface Reservation {
  readonly rideId: string;
  readonly txId: string;
  readonly status: Ride['status'];
  /** Set when a simulated concurrent claim was rejected in this user's favour. */
  readonly rivalRejected?: PassengerName;
}

function newRide(w: World, req: SeatRequest, now: number): [World, Ride] {
  const [next, txId] = nextTx(w);
  const soloDistanceM = roadDistanceM(req.pickup, req.dropoff);
  const ride: Ride = {
    id: `ride-${txId.toLowerCase()}`,
    txId,
    passenger: req.passenger,
    pickupZone: req.pickup,
    dropoffZone: req.dropoff,
    pickupLandmark: landmarkFor(req.passenger, req.pickup, 'PICKUP'),
    dropoffLandmark: landmarkFor(req.passenger, req.dropoff, 'DROPOFF'),
    seats: [],
    requestedSeat: req.seatIndex,
    status: 'REQUESTED',
    soloDistanceM,
    fare: computeFare({ distanceM: soloDistanceM, seats: 1, pooled: false }),
    poolTripId: null,
    joinedExistingPool: false,
    declinedByDriver: false,
    offerCooldownUntil: 0,
    requestedAt: now,
  };
  return [putRide({ ...next, current: { ...next.current, [req.passenger]: ride.id } }, ride), ride];
}

export function reserveSeat(w: World, req: SeatRequest, now: number): Outcome<Reservation> {
  if (req.pickup === req.dropoff) return fail(w, { code: 'SAME_ZONE' });
  const mine = currentRide(w, req.passenger);
  if (mine && isLive(mine.status)) return fail(w, { code: 'ALREADY_ACTIVE' });

  const seat = w.seats[req.seatIndex];
  if (seat.rideId) {
    // Optimistic lock lost: someone's transaction landed first. Record it and keep the rider searching.
    const holder = w.rides[seat.rideId]!;
    const msAgo = now - (seat.lockedAt ?? now);
    let next = { ...w, contention: { ...w.contention, [req.passenger]: { seatIndex: req.seatIndex, claimedBy: holder.passenger, msAgo, winnerTx: seat.txId ?? '', at: now } } };
    const [withRide] = newRide(next, { ...req }, now);
    next = putRide(withRide, { ...withRide.rides[withRide.current[req.passenger]!]!, requestedSeat: null });
    next = notify(
      next,
      req.passenger,
      'danger',
      `Seat ${req.seatIndex} was claimed by another commuter ${msAgo}ms ago. Searching for the next available pooled Tesla...`,
      now,
      true,
    );
    return fail(settle(next, now), { code: 'SEAT_TAKEN', seatIndex: req.seatIndex, claimedBy: holder.passenger, msAgo });
  }
  if (w.pool?.departed) return fail(w, { code: 'POOL_DEPARTED' });

  if (w.pool) {
    const plan = planJoin(w, req.pickup, req.dropoff);
    if (!plan.ok) return fail(w, { code: 'ROUTE_INCOMPATIBLE', reason: plan.reason });
    const [next, ride] = newRide(w, req, now);
    const joined = joinPool(next, ride, req.seatIndex, plan, now);
    return ok(settle(joined, now), { rideId: ride.id, txId: ride.txId, status: 'MATCHED' });
  }

  // No pool yet: hold the seat and ask Jashim.
  let [next, ride] = newRide(w, req, now);
  ride = { ...ride, seats: [req.seatIndex] };
  next = putRide(next, ride);
  next = setSeats(next, { [req.seatIndex]: { rideId: ride.id, lockedAt: now, txId: ride.txId } });
  next = notify(
    next,
    req.passenger,
    'info',
    next.vehicle.online ? `Seat ${req.seatIndex} held. Waiting for Jashim to accept.` : `Seat ${req.seatIndex} held. Jashim is offline; you'll be matched when he comes online.`,
    now,
  );
  return ok(settle(next, now), { rideId: ride.id, txId: ride.txId, status: 'REQUESTED' });
}

/** The competing claim that "Simulate Shirin Concurrent Booking" fires. */
function rivalClaim(w: World, rival: PassengerName, seatIndex: SeatIndex, at: number): World {
  if (w.seats[seatIndex].rideId) return w;
  const mine = currentRide(w, rival);
  if (mine && isLive(mine.status)) {
    // Already riding in this pool: the rival books the seat for a companion on the same ride.
    if (!w.pool || w.pool.departed || mine.poolTripId !== w.pool.id) return w;
    const [next, txId] = nextTx(w);
    const seats = [...mine.seats, seatIndex].sort() as SeatIndex[];
    let out = putRide(next, { ...mine, seats, fare: computeFare({ distanceM: mine.soloDistanceM, seats: seats.length, pooled: true }) });
    out = setSeats(out, { [seatIndex]: { rideId: mine.id, lockedAt: at, txId } });
    out = notify(out, rival, 'success', `Seat ${seatIndex} secured for your companion (${txId}).`, at);
    return notify(out, 'driver', 'info', `${rival} booked seat ${seatIndex} for a companion.`, at);
  }
  const p = PASSENGERS[rival];
  return reserveSeat(w, { passenger: rival, seatIndex, pickup: p.pickupZone, dropoff: p.dropoffZone }, at).world;
}

/**
 * Two claims for the same seat inside one network round trip. The server serialises them: Nusrat's
 * transaction always lands first (the PRD story), so Shirin loses by CONTENTION_GAP_MS; anyone else
 * racing Shirin wins and Shirin is queued.
 */
export function reserveSeatRace(w: World, req: SeatRequest, now: number): Outcome<Reservation> {
  if (req.passenger === 'Shirin') {
    return reserveSeat(rivalClaim(w, 'Nusrat', req.seatIndex, now - CONTENTION_GAP_MS), req, now);
  }
  const first = reserveSeat(w, req, now);
  if (!first.result.ok) return first;
  const shirin = PASSENGERS.Shirin;
  const second = reserveSeat(first.world, { passenger: 'Shirin', seatIndex: req.seatIndex, pickup: shirin.pickupZone, dropoff: shirin.dropoffZone }, now + CONTENTION_GAP_MS);
  const rivalRejected = !second.result.ok && second.result.error.code === 'SEAT_TAKEN' ? ('Shirin' as const) : undefined;
  let next = second.world;
  if (rivalRejected) next = notify(next, req.passenger, 'info', `Shirin tried to claim seat ${req.seatIndex} ${CONTENTION_GAP_MS}ms after you. Your lock held.`, now);
  return ok(next, { ...first.result.value, rivalRejected });
}

function applyCancel(w: World, ride: Ride, by: Actor, reason: string, now: number): World {
  let next = putRide(releaseSeats(w, ride.id), { ...ride, status: 'CANCELLED', seats: [], cancelledAt: now, cancelledBy: by, cancelReason: reason });
  if (next.offer?.rideId === ride.id) next = { ...next, offer: null };
  if (ride.poolTripId) {
    next = notify(next, 'driver', 'warning', by === 'DRIVER' ? `${ride.passenger} marked as no-show. Seat released.` : `${ride.passenger} cancelled. Seat capacity restored.`, now);
  }
  next = notify(next, ride.passenger, 'info', by === 'DRIVER' ? 'Jashim released your seat after waiting at the pickup.' : 'Ride cancelled. No penalty was charged.', now);
  return settle(next, now);
}

export function cancelRide(w: World, passenger: PassengerName, reason: string, now: number): Outcome<null> {
  const ride = currentRide(w, passenger);
  if (!ride || !isLive(ride.status)) return fail(w, { code: 'NO_RIDE' });
  if (!canTransition(ride.status, 'CANCELLED', 'PASSENGER', { now, arrivedAt: ride.arrivedAt })) {
    return fail(w, ride.status === 'DRIVER_ARRIVED' ? { code: 'CANCEL_WINDOW_CLOSED' } : { code: 'INVALID_TRANSITION', message: `A ${ride.status} ride can't be cancelled by the passenger.` });
  }
  return ok(applyCancel(w, ride, 'PASSENGER', reason, now), null);
}

export function rateRide(w: World, passenger: PassengerName, stars: number, now: number): Outcome<null> {
  const ride = currentRide(w, passenger);
  if (!ride || ride.status !== 'COMPLETED') return fail(w, { code: 'NO_RIDE' });
  const rating = Math.min(5, Math.max(1, Math.round(stars)));
  return ok(notify(putRide(w, { ...ride, rating }), passenger, 'success', `Thanks! You rated Jashim ${rating} of 5.`, now), null);
}

/** Leave a finished ride's summary and go back to the booking view (route draft is kept by the UI). */
export function startNewBooking(w: World, passenger: PassengerName): World {
  const ride = currentRide(w, passenger);
  if (ride && isLive(ride.status)) return w;
  return { ...w, current: { ...w.current, [passenger]: null }, contention: { ...w.contention, [passenger]: null } };
}

export function dismissContention(w: World, passenger: PassengerName): World {
  return w.contention[passenger] ? { ...w, contention: { ...w.contention, [passenger]: null } } : w;
}

// ---- driver commands ---------------------------------------------------------------------------------------------

export function setOnline(w: World, online: boolean, now: number): Outcome<null> {
  if (w.vehicle.online === online) return ok(w, null);
  if (!online && w.pool) return fail(w, { code: 'DRIVER_HAS_LIVE_POOL' });
  let next: World = { ...w, vehicle: { ...w.vehicle, online } };
  if (!online && next.offer) {
    const ride = next.rides[next.offer.rideId]!;
    next = putRide(releaseSeats({ ...next, offer: null }, ride.id), { ...ride, seats: [] });
  }
  next = notify(next, 'driver', online ? 'success' : 'info', online ? 'You are online. Bullet is visible to commuters.' : 'You are offline. No new ride requests.', now);
  return ok(settle(next, now), null);
}

export function acceptOffer(w: World, now: number): Outcome<string> {
  const offer = w.offer;
  const ride = offer ? w.rides[offer.rideId] : undefined;
  if (!offer || !ride || ride.status !== 'REQUESTED') return fail(w, { code: 'NO_OFFER' });
  if (!canTransition('REQUESTED', 'MATCHED', 'DRIVER')) return fail(w, { code: 'INVALID_TRANSITION', message: 'REQUESTED → MATCHED' });
  const poolId = `POOL-${w.seq.pool}`;
  let next: World = {
    ...w,
    offer: null,
    seq: { ...w.seq, pool: w.seq.pool + 1 },
    pool: { id: poolId, pickupZone: ride.pickupZone, departed: false, dropOrder: [ride.id], openedAt: now },
  };
  next = putRide(next, { ...ride, status: 'MATCHED', matchedAt: now, poolTripId: poolId });
  next = notify(next, ride.passenger, 'success', `Matched! Jashim is bringing Bullet to ${ride.pickupLandmark}.`, now);
  return ok(settle(next, now), ride.id);
}

export function declineOffer(w: World, now: number): Outcome<null> {
  const offer = w.offer;
  const ride = offer ? w.rides[offer.rideId] : undefined;
  if (!offer || !ride) return fail(w, { code: 'NO_OFFER' });
  let next = putRide(releaseSeats({ ...w, offer: null }, ride.id), { ...ride, seats: [], declinedByDriver: true });
  next = notify(next, ride.passenger, 'warning', 'Bullet could not take this ride. Still searching for a pooled Tesla...', now);
  return ok(settle(next, now), null);
}

export type StopKind = 'PICKUP' | 'DROPOFF';

export interface DriverStop {
  readonly id: string;
  readonly seq: number;
  readonly kind: StopKind;
  readonly rideId: string;
  readonly passenger: PassengerName;
  readonly zone: DhakaZone;
  readonly landmark: string;
  readonly rideStatus: Ride['status'];
  readonly state: 'done' | 'current' | 'upcoming';
  readonly isFinal: boolean;
}

/** The sequential navigation pipeline: every pickup (in match order), then drop-offs in planned order. */
export function driverStops(w: World): DriverStop[] {
  if (!w.pool) return [];
  const members = poolMembers(w);
  const byId = new Map(members.map((r) => [r.id, r]));
  const raw: Omit<DriverStop, 'seq' | 'state' | 'isFinal'>[] = [
    ...members.map((r) => ({ id: `${r.id}:PICKUP`, kind: 'PICKUP' as const, rideId: r.id, passenger: r.passenger, zone: r.pickupZone, landmark: r.pickupLandmark, rideStatus: r.status })),
    ...w.pool.dropOrder
      .map((id) => byId.get(id))
      .filter((r): r is Ride => !!r)
      .map((r) => ({ id: `${r.id}:DROPOFF`, kind: 'DROPOFF' as const, rideId: r.id, passenger: r.passenger, zone: r.dropoffZone, landmark: r.dropoffLandmark, rideStatus: r.status })),
  ];
  const isDone = (s: (typeof raw)[number]) =>
    s.kind === 'PICKUP' ? s.rideStatus === 'IN_TRANSIT' || s.rideStatus === 'COMPLETED' : s.rideStatus === 'COMPLETED';
  const currentIdx = raw.findIndex((s) => !isDone(s));
  const lastOpen = raw.reduce((acc, s, i) => (isDone(s) ? acc : i), -1);
  return raw.map((s, i) => ({
    ...s,
    seq: i + 1,
    state: isDone(s) ? 'done' : i === currentIdx ? 'current' : 'upcoming',
    isFinal: i === lastOpen,
  }));
}

function requireCurrentStop(w: World, rideId: string, kind: StopKind): DriverStop | Rejection {
  const stop = driverStops(w).find((s) => s.state === 'current');
  if (!stop || stop.rideId !== rideId || stop.kind !== kind) return { code: 'NOT_CURRENT_STOP' };
  return stop;
}

function driverMove(w: World, rideId: string, kind: StopKind, to: RideLifecycleStatus): Rejection | Ride {
  const stop = requireCurrentStop(w, rideId, kind);
  if ('code' in stop) return stop;
  const ride = w.rides[rideId]!;
  if (!canTransition(ride.status, to, 'DRIVER')) return { code: 'INVALID_TRANSITION', message: `${ride.status} → ${to}` };
  return ride;
}

export function confirmArrival(w: World, rideId: string, now: number): Outcome<null> {
  const ride = driverMove(w, rideId, 'PICKUP', 'DRIVER_ARRIVED');
  if ('code' in ride) return fail(w, ride);
  let next = putRide(w, { ...ride, status: 'DRIVER_ARRIVED', arrivedAt: now });
  next = { ...next, vehicle: { ...next.vehicle, currentZone: ride.pickupZone } };
  next = notify(next, ride.passenger, 'success', `Jashim has arrived at ${ride.pickupLandmark}. Look for Bullet, plate ${BULLET.plate}.`, now);
  return ok(next, null);
}

export function confirmBoarding(w: World, rideId: string, now: number): Outcome<null> {
  const ride = driverMove(w, rideId, 'PICKUP', 'IN_TRANSIT');
  if ('code' in ride) return fail(w, ride);
  let next = putRide(w, { ...ride, status: 'IN_TRANSIT', startedAt: now });
  next = {
    ...next,
    pool: next.pool ? { ...next.pool, departed: true } : null,
    vehicle: { ...next.vehicle, batteryPercent: Math.max(0, next.vehicle.batteryPercent - 1) },
    trafficTickAt: now,
  };
  next = notify(next, ride.passenger, 'info', `On board. Heading to ${ride.dropoffLandmark}.`, now);
  return ok(next, null);
}

export function completeStop(w: World, rideId: string, now: number): Outcome<LedgerEntry> {
  const ride = driverMove(w, rideId, 'DROPOFF', 'COMPLETED');
  if ('code' in ride) return fail(w, ride);
  const receiptId = `TP-${w.seq.receipt}`;
  const done: Ride = { ...ride, status: 'COMPLETED', completedAt: now, receiptId };
  const entry: LedgerEntry = {
    receiptId,
    rideId: ride.id,
    txId: ride.txId,
    passenger: ride.passenger,
    route: routeLabel(ride),
    netPoysha: ride.fare.netPoysha,
    pooled: ride.fare.pooled,
    settledAt: now,
  };
  let next = putRide(releaseSeats(w, ride.id), done);
  next = {
    ...next,
    ledger: [entry, ...next.ledger],
    seq: { ...next.seq, receipt: next.seq.receipt + 1 },
    vehicle: { ...next.vehicle, currentZone: ride.dropoffZone, batteryPercent: Math.max(0, next.vehicle.batteryPercent - 2) },
  };
  next = notify(next, ride.passenger, 'success', `Arrived at ${ride.dropoffLandmark}. ${formatTakaSymbol(entry.netPoysha)} settled with TeslaPay (${receiptId}).`, now);
  next = notify(next, 'driver', 'success', `${ride.passenger} dropped off. ${formatTakaSymbol(entry.netPoysha)} added to today's ledger.`, now);
  return ok(settle(next, now), entry);
}

export function markNoShow(w: World, rideId: string, now: number): Outcome<null> {
  const ride = driverMove(w, rideId, 'PICKUP', 'CANCELLED');
  if ('code' in ride) return fail(w, ride);
  if (ride.status !== 'DRIVER_ARRIVED') return fail(w, { code: 'INVALID_TRANSITION', message: 'Only an arrived pickup can be a no-show.' });
  const waited = now - (ride.arrivedAt ?? now);
  if (waited < NO_SHOW_WAIT_MS) return fail(w, { code: 'NO_SHOW_TOO_EARLY', remainingMs: NO_SHOW_WAIT_MS - waited });
  return ok(applyCancel(w, ride, 'DRIVER', 'Passenger no-show', now), null);
}

// ---- time --------------------------------------------------------------------------------------------------------

/** Called every second by the engine: offer expiry, re-offers and traffic. Returns `w` itself when nothing changed. */
export function tick(w: World, now: number): World {
  let next = w;
  if (next.offer && now >= next.offer.expiresAt) {
    const ride = next.rides[next.offer.rideId]!;
    next = putRide(releaseSeats({ ...next, offer: null }, ride.id), { ...ride, seats: [], offerCooldownUntil: now + REOFFER_DELAY_MS });
    next = notify(next, 'driver', 'warning', `Missed ride request from ${ride.passenger}. It will be offered again shortly.`, now);
  }
  next = nextOffer(next, now);
  if (next.pool?.departed && now - next.trafficTickAt >= TRAFFIC_INTERVAL_MS) {
    const idx = (TRAFFIC_CYCLE_SEC.indexOf(next.trafficDelaySec as (typeof TRAFFIC_CYCLE_SEC)[number]) + 1) % TRAFFIC_CYCLE_SEC.length;
    const delay = TRAFFIC_CYCLE_SEC[idx]!;
    next = { ...next, trafficDelaySec: delay, trafficTickAt: now };
    for (const r of poolMembers(next).filter((m) => m.status === 'IN_TRANSIT')) {
      next = notify(
        next,
        r.passenger,
        delay ? 'warning' : 'info',
        delay ? `Traffic building on the corridor: ETA +${Math.round(delay / 60)} min.` : 'Traffic cleared. ETA back on schedule.',
        now,
      );
    }
  }
  return next;
}

// ---- ETAs --------------------------------------------------------------------------------------------------------

/** Wall-clock time the passenger's next milestone is expected, or null when nothing is being waited on. */
export function etaFor(w: World, ride: Ride): number | null {
  if (ride.status === 'MATCHED' && ride.matchedAt !== undefined) return ride.matchedAt + PICKUP_ETA_MS;
  if (ride.status === 'IN_TRANSIT' && ride.startedAt !== undefined) {
    const inVehicle = rideDetour(w, ride.id)?.inVehicleM ?? ride.soloDistanceM;
    return ride.startedAt + Math.round((inVehicle / SPEED_M_PER_MIN) * 60_000) + w.trafficDelaySec * 1000;
  }
  return null;
}
