/**
 * DispatchEngine: the async shell around the pure world model.
 *
 * - Commands resolve after simulated network latency, so every control really passes through its
 *   Loading state (and the contention race takes exactly CONTENTION_GAP_MS).
 * - "Flaky network" makes every third command fail with NETWORK, to exercise Error states and retries.
 * - A 1 s ticker drives offer countdowns, re-offers and traffic.
 * - Views subscribe with useSyncExternalStore (subscribe/getSnapshot).
 */
import type { DhakaZone, PassengerName } from '@/types/mobility';

import { createWorld, type ScenarioId } from './scenarios';
import * as model from './world';
import type { LedgerEntry, Outcome, Quote, Reservation, Result, SeatRequest, World } from './world';

export interface EngineOptions {
  readonly now?: () => number;
  readonly latencyMs?: number;
  readonly scenario?: ScenarioId;
}

export class DispatchEngine {
  private world: World;
  private readonly listeners = new Set<() => void>();
  private readonly now: () => number;
  private readonly latencyMs: number;
  private ticker: ReturnType<typeof setInterval> | null = null;
  private flaky = false;
  private commandCount = 0;

  constructor(opts: EngineOptions = {}) {
    this.now = opts.now ?? Date.now;
    this.latencyMs = opts.latencyMs ?? 450;
    this.world = createWorld(opts.scenario ?? 'pool-forming', this.now());
  }

  // ---- store -------------------------------------------------------------------------------------------------------

  readonly getSnapshot = (): World => this.world;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private commit(next: World): void {
    if (next === this.world) return;
    this.world = next;
    for (const l of this.listeners) l();
  }

  start(): void {
    if (this.ticker) return;
    this.ticker = setInterval(() => this.commit(model.tick(this.world, this.now())), 1000);
  }

  stop(): void {
    if (this.ticker) clearInterval(this.ticker);
    this.ticker = null;
  }

  get isFlaky(): boolean {
    return this.flaky;
  }

  setFlaky(flaky: boolean): void {
    this.flaky = flaky;
    this.commandCount = 0;
  }

  reset(scenario: ScenarioId): void {
    const fresh = createWorld(scenario, this.now());
    // Notice ids keep counting across resets so listeners never mistake new notices for old ones.
    this.commit({ ...fresh, seq: { ...fresh.seq, notice: this.world.seq.notice } });
  }

  // ---- command plumbing --------------------------------------------------------------------------------------------

  private async run<T>(fn: (w: World, now: number) => Outcome<T>, latency = this.latencyMs): Promise<Result<T>> {
    await new Promise((resolve) => setTimeout(resolve, latency));
    if (this.flaky && ++this.commandCount % 3 === 0) return { ok: false, error: { code: 'NETWORK' } };
    const { world, result } = fn(this.world, this.now());
    this.commit(world);
    return result;
  }

  private local(fn: (w: World) => World): void {
    this.commit(fn(this.world));
  }

  // ---- passenger ---------------------------------------------------------------------------------------------------

  quote(pickup: DhakaZone, dropoff: DhakaZone): Promise<Result<Quote>> {
    return this.run((w) => model.quote(w, pickup, dropoff), 300);
  }

  reserveSeat(req: SeatRequest, opts: { simulateRace?: boolean } = {}): Promise<Result<Reservation>> {
    return opts.simulateRace
      ? this.run((w, now) => model.reserveSeatRace(w, req, now), model.CONTENTION_GAP_MS)
      : this.run((w, now) => model.reserveSeat(w, req, now));
  }

  cancelRide(passenger: PassengerName, reason: string): Promise<Result<null>> {
    return this.run((w, now) => model.cancelRide(w, passenger, reason, now));
  }

  rateRide(passenger: PassengerName, stars: number): Promise<Result<null>> {
    return this.run((w, now) => model.rateRide(w, passenger, stars, now));
  }

  startNewBooking(passenger: PassengerName): void {
    this.local((w) => model.startNewBooking(w, passenger));
  }

  dismissContention(passenger: PassengerName): void {
    this.local((w) => model.dismissContention(w, passenger));
  }

  // ---- driver ------------------------------------------------------------------------------------------------------

  setOnline(online: boolean): Promise<Result<null>> {
    return this.run((w, now) => model.setOnline(w, online, now));
  }

  acceptOffer(): Promise<Result<string>> {
    return this.run((w, now) => model.acceptOffer(w, now));
  }

  declineOffer(): Promise<Result<null>> {
    return this.run((w, now) => model.declineOffer(w, now));
  }

  confirmArrival(rideId: string): Promise<Result<null>> {
    return this.run((w, now) => model.confirmArrival(w, rideId, now));
  }

  confirmBoarding(rideId: string): Promise<Result<null>> {
    return this.run((w, now) => model.confirmBoarding(w, rideId, now));
  }

  completeStop(rideId: string): Promise<Result<LedgerEntry>> {
    return this.run((w, now) => model.completeStop(w, rideId, now));
  }

  markNoShow(rideId: string): Promise<Result<null>> {
    return this.run((w, now) => model.markNoShow(w, rideId, now));
  }
}
