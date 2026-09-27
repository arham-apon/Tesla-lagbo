'use client';

import { ArrowDownUp, CircleDot, MapPin } from 'lucide-react';

import { Button } from '@/components/ui/Button';
import { SelectField } from '@/components/ui/controls';
import { Panel } from '@/components/ui/primitives';
import type { DhakaZone } from '@/types/mobility';
import { formatKm, roadDistanceM, ZONES } from '@/domain/zones';
import { planJoin, type World } from '@/sim/world';
import { cx } from '@/utils/cx';

export interface RouteSelectorProps {
  readonly world: World;
  readonly pickup: DhakaZone;
  readonly dropoff: DhakaZone;
  readonly onChange: (route: { pickup: DhakaZone; dropoff: DhakaZone }) => void;
  /** Locked while a ride is live: shows the ride's route instead of the draft. */
  readonly locked: boolean;
}

/** Pickup / drop-off from the predefined Dhaka transit zones, with an honest detour preview. */
export function RouteSelector({ world, pickup, dropoff, onChange, locked }: RouteSelectorProps) {
  const poolZone = world.pool && !world.pool.departed ? world.pool.pickupZone : null;
  const options = (other: DhakaZone, isPickup: boolean) =>
    ZONES.map((z) => ({
      value: z.name,
      label: z.name,
      hint: isPickup && z.name === poolZone ? 'Bullet pool' : undefined,
      disabled: z.name === other,
    }));

  const plan = !locked && poolZone ? planJoin(world, pickup, dropoff) : null;
  const direct = pickup === dropoff ? 0 : roadDistanceM(pickup, dropoff);

  return (
    <Panel title="Corridor route" titleId="route-title">
      <div className="flex items-end gap-2">
        <div className="grid flex-1 gap-3 sm:grid-cols-2">
          <SelectField
            label="Pickup"
            value={pickup}
            options={options(dropoff, true)}
            onValueChange={(v) => onChange({ pickup: v as DhakaZone, dropoff })}
            disabled={locked}
            icon={<CircleDot className="size-4 text-mint" aria-hidden />}
          />
          <SelectField
            label="Drop-off"
            value={dropoff}
            options={options(pickup, false)}
            onValueChange={(v) => onChange({ pickup, dropoff: v as DhakaZone })}
            disabled={locked}
            icon={<MapPin className="size-4 text-amber" aria-hidden />}
          />
        </div>
        <Button
          variant="secondary"
          aria-label="Swap pickup and drop-off"
          disabled={locked}
          onClick={() => onChange({ pickup: dropoff, dropoff: pickup })}
          icon={<ArrowDownUp className="size-4" aria-hidden />}
        />
      </div>
      <p className="mt-3 font-mono text-caption-sm tabular-nums text-fg-secondary">Direct distance {formatKm(direct)}</p>
      {plan && (
        <p className={cx('mt-1 text-caption-sm', plan.ok ? 'text-mint' : 'text-amber')}>
          {plan.ok
            ? plan.addedM > 0
              ? `Shares Bullet's route: adds ${formatKm(plan.addedM)} to the pool, longest co-rider detour ${plan.maxDetourPct}% (cap 140%).`
              : "Shares Bullet's route with no extra distance."
            : plan.reason}
        </p>
      )}
      {locked && <p className="mt-1 text-caption-sm text-fg-secondary">Route is locked while your ride is live.</p>}
    </Panel>
  );
}
