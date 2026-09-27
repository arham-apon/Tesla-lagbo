import type { Rejection } from '@/sim/world';
import { formatClock } from '@/utils/time';

/** One place that turns a server rejection into words a commuter or driver can act on. */
export function describeRejection(e: Rejection): string {
  switch (e.code) {
    case 'SEAT_TAKEN':
      return `Seat ${e.seatIndex} was claimed by another commuter ${e.msAgo}ms ago. Searching for the next available pooled Tesla...`;
    case 'SAME_ZONE':
      return 'Pickup and drop-off must be different zones.';
    case 'ALREADY_ACTIVE':
      return 'You already have a live ride.';
    case 'POOL_DEPARTED':
      return 'Bullet has already left with its riders. New seats open when the pool finishes.';
    case 'ROUTE_INCOMPATIBLE':
      return e.reason;
    case 'DRIVER_OFFLINE':
      return 'Jashim is offline.';
    case 'NO_RIDE':
      return 'There is no live ride to change.';
    case 'NO_OFFER':
      return 'That ride request is no longer open.';
    case 'CANCEL_WINDOW_CLOSED':
      return 'The free cancellation window has closed. Jashim is waiting at your pickup.';
    case 'INVALID_TRANSITION':
      return `That step isn't allowed right now (${e.message}).`;
    case 'NOT_CURRENT_STOP':
      return 'Finish the current stop first. Stops run in order.';
    case 'NO_SHOW_TOO_EARLY':
      return `Wait ${formatClock(e.remainingMs)} more before marking a no-show.`;
    case 'DRIVER_HAS_LIVE_POOL':
      return 'Finish or release your current riders before going offline.';
    case 'NETWORK':
      return 'Network hiccup — nothing was changed. Try again.';
  }
}
