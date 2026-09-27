import { poolPath } from '@/components/map/CorridorMap';
import type { World } from '@/sim/world';

/** "Corridor: Banani → Gulshan 1 → Mohakhali", or where Bullet is waiting when it has no pool. */
export function CorridorLabel({ world }: { world: World }) {
  const path = poolPath(world);
  return <span>Corridor: {path.length ? path.join(' → ') : `waiting at ${world.vehicle.currentZone}`}</span>;
}
