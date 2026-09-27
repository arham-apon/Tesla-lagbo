import { PASSENGERS } from '@/domain/cast';

import { acceptOffer, emptyWorld, reserveSeat, type World } from './world';

export type ScenarioId = 'pool-forming' | 'empty-bullet';

export const SCENARIOS: readonly { id: ScenarioId; label: string; description: string }[] = [
  {
    id: 'pool-forming',
    label: 'Pool forming',
    description: 'Nusrat (seat 1) and Rafiq (seat 2) are matched on Bullet. Seat 3 is the last one open.',
  },
  {
    id: 'empty-bullet',
    label: 'Empty Bullet',
    description: 'Jashim is online with no riders. The first booking needs his acceptance.',
  },
];

/**
 * Scenarios are built by replaying real commands through the model, never by hand-writing state,
 * so a seeded world is always one the rules could have produced.
 */
export function createWorld(scenario: ScenarioId, now: number): World {
  let w = emptyWorld(now);
  if (scenario === 'pool-forming') {
    const n = PASSENGERS.Nusrat;
    const r = PASSENGERS.Rafiq;
    w = reserveSeat(w, { passenger: 'Nusrat', seatIndex: 1, pickup: n.pickupZone, dropoff: n.dropoffZone }, now - 120_000).world;
    w = acceptOffer(w, now - 110_000).world;
    w = reserveSeat(w, { passenger: 'Rafiq', seatIndex: 2, pickup: r.pickupZone, dropoff: r.dropoffZone }, now - 45_000).world;
  }
  return { ...w, notices: [] };
}
