import type { Actor, RideLifecycleStatus } from '@/types/mobility';

/**
 * The unified ride lifecycle state machine. Views never keep isLoading / isMatched / hasArrived flags:
 * they derive everything from one RideLifecycleStatus, and every move goes through `canTransition`.
 *
 * Actors mirror the trip service's TRANSITIONS table (STARTED there = IN_TRANSIT here), plus the
 * guideline's "restricted cancellation window" for passengers once the driver has arrived.
 */

type Rule = { readonly from: RideLifecycleStatus; readonly to: RideLifecycleStatus; readonly actors: readonly Actor[] };

export const TRANSITIONS: readonly Rule[] = [
  { from: 'IDLE', to: 'REQUESTED', actors: ['PASSENGER'] },
  { from: 'REQUESTED', to: 'MATCHED', actors: ['SYSTEM', 'DRIVER'] },
  { from: 'REQUESTED', to: 'CANCELLED', actors: ['PASSENGER', 'SYSTEM'] },
  { from: 'MATCHED', to: 'DRIVER_ARRIVED', actors: ['DRIVER'] },
  { from: 'MATCHED', to: 'CANCELLED', actors: ['PASSENGER', 'DRIVER'] },
  { from: 'DRIVER_ARRIVED', to: 'IN_TRANSIT', actors: ['DRIVER'] },
  { from: 'DRIVER_ARRIVED', to: 'CANCELLED', actors: ['DRIVER', 'PASSENGER'] },
  { from: 'IN_TRANSIT', to: 'COMPLETED', actors: ['DRIVER'] },
];

/** A passenger may still cancel for this long after the driver reports arrival. */
export const ARRIVAL_CANCEL_GRACE_MS = 60_000;

export interface TransitionContext {
  readonly now?: number;
  readonly arrivedAt?: number;
}

export function canTransition(
  from: RideLifecycleStatus,
  to: RideLifecycleStatus,
  actor: Actor,
  ctx: TransitionContext = {},
): boolean {
  const rule = TRANSITIONS.find((r) => r.from === from && r.to === to);
  if (!rule || !rule.actors.includes(actor)) return false;
  if (from === 'DRIVER_ARRIVED' && to === 'CANCELLED' && actor === 'PASSENGER') {
    if (ctx.now === undefined || ctx.arrivedAt === undefined) return false;
    return ctx.now - ctx.arrivedAt < ARRIVAL_CANCEL_GRACE_MS;
  }
  return true;
}

export class LifecycleError extends Error {
  constructor(
    readonly from: RideLifecycleStatus,
    readonly to: RideLifecycleStatus,
    readonly actor: Actor,
  ) {
    super(`${from} → ${to} is not allowed for ${actor}`);
    this.name = 'LifecycleError';
  }
}

export function assertTransition(from: RideLifecycleStatus, to: RideLifecycleStatus, actor: Actor, ctx?: TransitionContext): void {
  if (!canTransition(from, to, actor, ctx)) throw new LifecycleError(from, to, actor);
}

export const isTerminal = (s: RideLifecycleStatus): boolean => s === 'COMPLETED' || s === 'CANCELLED';
export const isLive = (s: RideLifecycleStatus): boolean => s !== 'IDLE' && !isTerminal(s);

export type CancelWindow =
  | { readonly allowed: true; readonly remainingMs: number | null }
  | { readonly allowed: false; readonly reason: string };

/** What the passenger's cancel control should say right now. */
export function passengerCancelWindow(status: RideLifecycleStatus, now: number | null, arrivedAt?: number): CancelWindow {
  switch (status) {
    case 'REQUESTED':
    case 'MATCHED':
      return { allowed: true, remainingMs: null };
    case 'DRIVER_ARRIVED': {
      if (now === null || arrivedAt === undefined) return { allowed: true, remainingMs: ARRIVAL_CANCEL_GRACE_MS };
      const remainingMs = ARRIVAL_CANCEL_GRACE_MS - (now - arrivedAt);
      return remainingMs > 0
        ? { allowed: true, remainingMs }
        : { allowed: false, reason: 'Jashim is waiting at your pickup. The free cancellation window has closed.' };
    }
    case 'IN_TRANSIT':
      return { allowed: false, reason: 'Cancellation is disabled while the ride is in transit.' };
    default:
      return { allowed: false, reason: 'There is no live ride to cancel.' };
  }
}

/** The four milestones of the passenger's Trip Progress Header. */
export const PROGRESS_STEPS = [
  { status: 'REQUESTED', label: 'Requested' },
  { status: 'MATCHED', label: 'Matched' },
  { status: 'IN_TRANSIT', label: 'In Transit' },
  { status: 'COMPLETED', label: 'Completed' },
] as const satisfies readonly { status: RideLifecycleStatus; label: string }[];

/** Index of the current milestone; DRIVER_ARRIVED is still the "Matched" milestone. -1 = not started. */
export function progressIndex(status: RideLifecycleStatus): number {
  switch (status) {
    case 'REQUESTED':
      return 0;
    case 'MATCHED':
    case 'DRIVER_ARRIVED':
      return 1;
    case 'IN_TRANSIT':
      return 2;
    case 'COMPLETED':
      return 3;
    default:
      return -1;
  }
}

export const STATUS_LABEL: Record<RideLifecycleStatus, string> = {
  IDLE: 'Ready to book',
  REQUESTED: 'Searching',
  MATCHED: 'Matched',
  DRIVER_ARRIVED: 'Driver arrived',
  IN_TRANSIT: 'In transit',
  COMPLETED: 'Completed',
  CANCELLED: 'Cancelled',
};
