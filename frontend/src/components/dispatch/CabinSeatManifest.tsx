'use client';

import * as RadioGroup from '@radix-ui/react-radio-group';
import { motion } from 'framer-motion';
import { BatteryCharging, CircleDashed, LoaderCircle, UserRound } from 'lucide-react';

import { CorridorLabel } from '@/components/passenger/CorridorLabel';
import { Badge, Panel } from '@/components/ui/primitives';
import { BULLET } from '@/domain/cast';
import type { PassengerName, SeatIndex } from '@/types/mobility';
import { formatTakaSymbol } from '@/domain/money';
import { occupiedSeatCount, seatRide } from '@/sim/selectors';
import { SEAT_INDICES, type Contention, type Ride, type World } from '@/sim/world';
import { cx } from '@/utils/cx';

export interface CabinSeatManifestProps {
  readonly world: World;
  readonly persona: PassengerName;
  readonly selectedSeat: SeatIndex | null;
  readonly onSelect: (seat: SeatIndex) => void;
  /** Why the open seats can't be booked right now (null = bookable). */
  readonly blocker: string | null;
  readonly blockerId: string;
  /** Seat whose reservation is in flight ("Securing Seat..."). */
  readonly securingSeat: SeatIndex | null;
  readonly contention: Contention | null;
}

type SeatKind = 'open' | 'mine' | 'held' | 'taken';

function kindOf(ride: Ride | null, persona: PassengerName): SeatKind {
  if (!ride) return 'open';
  if (ride.status === 'REQUESTED') return 'held';
  return ride.passenger === persona ? 'mine' : 'taken';
}

const chip: Record<SeatKind, { label: string; tone: 'mint' | 'amber' | 'neutral' }> = {
  open: { label: 'AVAILABLE', tone: 'mint' },
  mine: { label: 'YOUR SEAT', tone: 'mint' },
  held: { label: 'HELD', tone: 'amber' },
  taken: { label: 'ASSIGNED', tone: 'neutral' },
};

const frame: Record<SeatKind, string> = {
  open: 'border-mint bg-surface-raised motion-safe:animate-seat-open',
  mine: 'border-mint bg-mint-subtle',
  held: 'border-amber bg-amber-subtle',
  taken: 'border-line-subtle bg-surface-elevated',
};

/** Bullet's 3-seat cabin: who sits where, and which seat is still open. */
export function CabinSeatManifest({ world, persona, selectedSeat, onSelect, blocker, blockerId, securingSeat, contention }: CabinSeatManifestProps) {
  const occupied = occupiedSeatCount(world);
  return (
    <Panel
      title={
        <span className="inline-flex items-center gap-2">
          {BULLET.vehicleIdentifier} cabin
          <Badge>{BULLET.kind}</Badge>
        </span>
      }
      titleId="cabin-title"
      action={
        <span className="font-mono text-telemetry-md tabular-nums text-fg-primary" aria-label={`${occupied} of 3 seats occupied`}>
          {occupied} / 3
        </span>
      }
    >
      <p className="mb-4 flex flex-wrap items-center gap-2 text-caption-sm text-fg-secondary">
        <BatteryCharging className="size-4 text-mint" aria-hidden />
        Operator: {BULLET.driverName} · <CorridorLabel world={world} /> · {world.vehicle.online ? 'Online' : 'Offline'}
      </p>

      <RadioGroup.Root
        aria-labelledby="cabin-title"
        aria-describedby={blocker ? blockerId : undefined}
        value={selectedSeat ? String(selectedSeat) : ''}
        onValueChange={(v) => onSelect(Number(v) as SeatIndex)}
        className="grid gap-3 sm:grid-cols-3"
      >
        {SEAT_INDICES.map((i) => {
          const ride = seatRide(world, i);
          const kind = kindOf(ride, persona);
          const contested = contention?.seatIndex === i;
          const securing = securingSeat === i && kind === 'open';
          return (
            <motion.div
              key={contested ? `${i}-${contention!.at}` : `${i}`}
              layout
              initial={contested ? { scale: 0.98 } : false}
              animate={{ scale: 1 }}
              className={cx(
                'relative flex min-h-touch flex-col rounded-lg border-2 transition duration-300',
                frame[kind],
                kind === 'open' && selectedSeat === i && 'bg-mint-subtle',
                contested && 'motion-safe:animate-contention-pulse',
              )}
            >
              {kind === 'open' ? (
                <RadioGroup.Item
                  value={String(i)}
                  disabled={!!blocker}
                  aria-busy={securing || undefined}
                  aria-label={`Seat ${i}, available`}
                  className="flex h-full min-h-touch w-full flex-col gap-3 rounded-md p-4 text-left transition duration-150 hover:brightness-108 active:scale-98 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <SeatHeader index={i} kind={kind} />
                  <span className="flex items-center gap-2 text-heading-lg text-fg-primary">
                    {securing ? <LoaderCircle className="size-4 text-mint motion-safe:animate-spin" aria-hidden /> : <CircleDashed className="size-4 text-mint" aria-hidden />}
                    {securing ? 'Securing Seat...' : 'Seat Open'}
                  </span>
                  <span className="text-caption-sm text-fg-secondary">{selectedSeat === i ? 'Selected' : 'Tap to select'}</span>
                </RadioGroup.Item>
              ) : (
                <div className="flex flex-col gap-3 p-4" aria-label={`Seat ${i}, ${chip[kind].label.toLowerCase()}${ride ? `, ${ride.passenger}` : ''}`} role="group">
                  <SeatHeader index={i} kind={kind} />
                  <OccupantDetails ride={ride!} kind={kind} seat={i} />
                </div>
              )}
            </motion.div>
          );
        })}
      </RadioGroup.Root>
      {blocker && (
        <p id={blockerId} className="mt-3 text-caption-sm text-fg-secondary">
          {blocker}
        </p>
      )}
    </Panel>
  );
}

function SeatHeader({ index, kind }: { index: SeatIndex; kind: SeatKind }) {
  return (
    <span className="flex flex-wrap items-center justify-between gap-2">
      <span className="font-mono text-caption-sm tabular-nums text-fg-secondary">SEAT 0{index}</span>
      <Badge tone={chip[kind].tone}>{chip[kind].label}</Badge>
    </span>
  );
}

function OccupantDetails({ ride, kind, seat }: { ride: Ride; kind: SeatKind; seat: SeatIndex }) {
  const companion = ride.seats.length > 1 && ride.seats[0] !== seat;
  return (
    <>
      <span className="flex items-center gap-2 text-heading-lg text-fg-primary">
        <UserRound className="size-4 text-fg-secondary" aria-hidden />
        {kind === 'mine' ? `You (${ride.passenger})` : ride.passenger}
        {companion && <span className="text-caption-sm text-fg-secondary">+ companion</span>}
      </span>
      {kind === 'held' ? (
        <Badge tone="amber">Awaiting Jashim</Badge>
      ) : (
        <Badge tone={kind === 'mine' ? 'mint' : 'neutral'}>En Route to {ride.dropoffZone}</Badge>
      )}
      {/* Billing stays private: only your own seat shows a fare. */}
      {kind === 'mine' && <span className="font-mono text-telemetry-md tabular-nums text-mint">{formatTakaSymbol(ride.fare.netPoysha)}</span>}
    </>
  );
}
