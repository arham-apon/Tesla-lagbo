'use client';

import * as RadioGroup from '@radix-ui/react-radio-group';
import { motion } from 'framer-motion';
import { BadgeCheck, CarFront, Clock, MapPin, ReceiptText, Star, TriangleAlert } from 'lucide-react';
import { useEffect, useRef } from 'react';

import { Badge, CLOCK_PLACEHOLDER, Panel, Skeleton, Telemetry } from '@/components/ui/primitives';
import { SeatPips } from '@/components/ui/controls';
import { BULLET } from '@/domain/cast';
import { passengerCancelWindow } from '@/domain/lifecycle';
import { formatPoysha, formatTakaSymbol } from '@/domain/money';
import { formatKm } from '@/domain/zones';
import { useNow } from '@/hooks/useNow';
import { occupiedSeatCount } from '@/sim/selectors';
import { driverStops, etaFor, NO_SHOW_WAIT_MS, rideDetour, type Ride, type World } from '@/sim/world';
import { useEngine } from '@/state/EngineProvider';
import { useCommand } from '@/state/useCommand';
import { cx } from '@/utils/cx';
import { vibrate, playMatchChime } from '@/utils/feedback';
import { formatClock, formatTimeOfDay } from '@/utils/time';

/** One card whose content is derived entirely from the ride's lifecycle status. */
export function RideStatusCard({ world, ride }: { world: World; ride: Ride }) {
  useArrivalFeedback(ride);
  switch (ride.status) {
    case 'REQUESTED':
      return <Searching world={world} ride={ride} />;
    case 'MATCHED':
      return <Matched world={world} ride={ride} />;
    case 'DRIVER_ARRIVED':
      return <Arrived ride={ride} />;
    case 'IN_TRANSIT':
      return <InTransit world={world} ride={ride} />;
    case 'COMPLETED':
      return <Completed ride={ride} />;
    case 'CANCELLED':
      return <Cancelled ride={ride} />;
  }
}

/** Chime on match, haptic buzz on arrival — only for transitions seen live, not on first render. */
function useArrivalFeedback(ride: Ride) {
  const prev = useRef<{ id: string; status: Ride['status'] } | null>(null);
  useEffect(() => {
    const before = prev.current;
    prev.current = { id: ride.id, status: ride.status };
    if (!before || before.id !== ride.id || before.status === ride.status) return;
    if (ride.status === 'MATCHED') playMatchChime();
    if (ride.status === 'DRIVER_ARRIVED') vibrate([200, 100, 200]);
  }, [ride.id, ride.status]);
}

function RouteLine({ ride }: { ride: Ride }) {
  return (
    <p className="flex flex-wrap items-center gap-2 text-body-md text-fg-primary">
      <MapPin className="size-4 text-mint" aria-hidden />
      <span>{ride.pickupLandmark}</span>
      <span aria-hidden className="text-fg-secondary">→</span>
      <span className="sr-only">to</span>
      <span>{ride.dropoffLandmark}</span>
    </p>
  );
}

function Searching({ world, ride }: { world: World; ride: Ride }) {
  const awaitingDriver = world.offer?.rideId === ride.id;
  const held = ride.seats.length > 0;
  return (
    <Panel title="Finding your pooled Tesla" titleId="status-title" aria-busy="true">
      <div className="flex items-center gap-4">
        <span className="relative grid size-16 shrink-0 place-items-center" aria-hidden>
          <span className="absolute inset-0 rounded-full border-2 border-amber motion-safe:animate-radar" />
          <span className="absolute inset-2 rounded-full border border-amber opacity-40" />
          <CarFront className="size-6 text-amber" />
        </span>
        <div className="flex flex-col gap-1">
          <RouteLine ride={ride} />
          <p className="text-caption-sm text-amber">
            {awaitingDriver
              ? `Seat ${ride.seats[0]} held. Waiting for Jashim to accept.`
              : held
                ? `Seat ${ride.seats[0]} held.`
                : 'Searching for the next available pooled Tesla...'}
          </p>
        </div>
      </div>
      {/* Non-blocking skeleton for the driver details that will appear here. */}
      <div className="mt-4 flex items-center gap-3 rounded-lg border border-line-subtle p-3" aria-hidden>
        <Skeleton className="size-12 shrink-0 rounded-full" />
        <div className="flex flex-1 flex-col gap-2">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-3 w-2/3" />
        </div>
      </div>
      <p className="mt-3 text-caption-sm text-fg-secondary">You can cancel without penalty while we search.</p>
    </Panel>
  );
}

function DriverIdentity() {
  return (
    <div className="flex items-center gap-3">
      <span className="grid size-12 shrink-0 place-items-center rounded-full bg-mint-subtle text-heading-lg text-mint" aria-hidden>
        J
      </span>
      <div className="flex flex-col">
        <span className="text-heading-lg text-fg-primary">{BULLET.driverName}</span>
        <span className="text-caption-sm text-fg-secondary">
          {BULLET.vehicleIdentifier} · {BULLET.kind}
        </span>
      </div>
      <Badge className="ml-auto font-mono">{BULLET.plate}</Badge>
    </div>
  );
}

function Matched({ world, ride }: { world: World; ride: Ride }) {
  const now = useNow();
  const eta = etaFor(world, ride);
  const remaining = now !== null && eta !== null ? eta - now : null;
  const detour = rideDetour(world, ride.id);
  const occupied = occupiedSeatCount(world);
  return (
    <Panel title="Matched with Jashim" titleId="status-title">
      <div className="flex flex-col gap-4">
        <DriverIdentity />
        <RouteLine ride={ride} />
        <dl className="grid grid-cols-2 gap-4">
          <Telemetry
            label="Pickup ETA"
            size="lg"
            tone="mint"
            value={remaining === null ? CLOCK_PLACEHOLDER : remaining > 0 ? formatClock(remaining) : 'Now'}
          />
          <Telemetry
            label="Seats on Bullet"
            value={
              <span className="inline-flex items-center gap-2">
                <SeatPips occupied={occupied} capacity={3} />
                {occupied} / 3
              </span>
            }
            srValue={`${occupied} of 3 seats taken`}
          />
        </dl>
        {detour && (
          <p className={cx('text-caption-sm', detour.pct > 100 ? 'text-amber' : 'text-fg-secondary')}>
            {detour.pct > 100
              ? `Sharing adds ${formatKm(detour.inVehicleM - detour.soloM)} to your trip (${detour.pct}% of direct, cap 140%).`
              : 'Sharing adds no distance to your trip.'}
          </p>
        )}
      </div>
    </Panel>
  );
}

function Arrived({ ride }: { ride: Ride }) {
  const now = useNow();
  const waited = now !== null && ride.arrivedAt !== undefined ? now - ride.arrivedAt : null;
  const cancelWin = passengerCancelWindow('DRIVER_ARRIVED', now, ride.arrivedAt);
  return (
    <motion.section
      aria-labelledby="status-title"
      initial={{ opacity: 0, scale: 0.98 }}
      animate={{ opacity: 1, scale: 1 }}
      className="flex flex-col gap-4 rounded-xl border-2 border-mint bg-mint p-4 text-fg-on-accent shadow-lifted"
    >
      <div className="flex items-start gap-3">
        <BadgeCheck className="mt-1 size-6 shrink-0" aria-hidden />
        <div className="flex flex-col gap-1">
          <h2 id="status-title" className="text-title-xl">
            Jashim has arrived
          </h2>
          <p className="text-body-md">
            At {ride.pickupLandmark}. Look for Bullet, plate <span className="font-mono font-bold">{BULLET.plate}</span>.
          </p>
        </div>
      </div>
      <dl className="flex flex-wrap items-end justify-between gap-4 rounded-lg bg-canvas p-3 text-fg-primary">
        <Telemetry label="Waiting for you" size="lg" value={waited === null ? CLOCK_PLACEHOLDER : formatClock(waited)} />
        <p className="text-caption-sm text-fg-secondary">
          {cancelWin.allowed ? `Free cancellation for ${formatClock(cancelWin.remainingMs ?? 0)} more` : 'Free cancellation window closed'}
        </p>
      </dl>
    </motion.section>
  );
}

function InTransit({ world, ride }: { world: World; ride: Ride }) {
  const now = useNow();
  const eta = etaFor(world, ride);
  const remaining = now !== null && eta !== null ? eta - now : null;
  const stops = driverStops(world);
  const myDrop = stops.findIndex((s) => s.rideId === ride.id && s.kind === 'DROPOFF');
  const milestones = stops.slice(0, myDrop + 1).filter((s) => s.kind === 'DROPOFF' || s.rideId === ride.id);
  const delayed = world.trafficDelaySec > 0;
  return (
    <Panel title="On the way" titleId="status-title" action={delayed ? <Badge tone="amber" icon={<TriangleAlert className="size-4" aria-hidden />}>Traffic +{Math.round(world.trafficDelaySec / 60)} min</Badge> : undefined}>
      <div className="flex flex-col gap-4">
        <dl className="grid grid-cols-2 gap-4">
          <Telemetry label={`ETA ${ride.dropoffLandmark}`} size="lg" tone={delayed ? 'amber' : 'mint'} value={remaining === null ? CLOCK_PLACEHOLDER : formatClock(remaining)} />
          <Telemetry label="Your fare" size="lg" value={formatTakaSymbol(ride.fare.netPoysha)} />
        </dl>
        <ol aria-label="Route milestones" className="flex flex-col gap-2">
          {milestones.map((s) => {
            const mine = s.rideId === ride.id;
            const done = s.state === 'done';
            return (
              <li key={s.id} className="flex items-center gap-3">
                <span aria-hidden className={cx('size-3 shrink-0 rounded-full border-2', done ? 'border-mint bg-mint' : s.state === 'current' ? 'border-amber' : 'border-line-strong')} />
                <span className={cx('text-body-md', done ? 'text-fg-secondary' : 'text-fg-primary')}>
                  {s.kind === 'PICKUP' ? 'Picked up' : mine ? 'Your drop-off' : 'Stop'} · {s.landmark}
                  {done && <span className="sr-only"> (done)</span>}
                </span>
              </li>
            );
          })}
        </ol>
        <p className="text-caption-sm text-fg-secondary">Cancellation is disabled while the ride is in transit.</p>
      </div>
    </Panel>
  );
}

function Completed({ ride }: { ride: Ride }) {
  const engine = useEngine();
  const rate = useCommand((stars: number) => engine.rateRide(ride.passenger, stars));
  return (
    <Panel title="Ride complete" titleId="status-title" action={<Badge tone="mint">Paid</Badge>}>
      <div className="flex flex-col gap-4">
        <RouteLine ride={ride} />
        <div className="flex items-start gap-3 rounded-lg border border-line-subtle bg-surface-elevated p-3">
          <ReceiptText className="mt-1 size-4 shrink-0 text-mint" aria-hidden />
          <dl className="grid flex-1 grid-cols-2 gap-3">
            <Telemetry label="TeslaPay receipt" value={ride.receiptId ?? '—'} />
            <Telemetry label="Settled" value={ride.completedAt ? formatTimeOfDay(ride.completedAt) : '—'} />
            <Telemetry label="Final fare" size="lg" tone="mint" value={formatTakaSymbol(ride.fare.netPoysha)} />
            <Telemetry label="In poysha" value={formatPoysha(ride.fare.netPoysha)} />
          </dl>
        </div>
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 text-body-md font-semibold text-fg-primary">How was your ride with Jashim?</legend>
          <RadioGroup.Root
            value={ride.rating ? String(ride.rating) : ''}
            onValueChange={(v) => void rate.run(Number(v))}
            orientation="horizontal"
            aria-busy={rate.pending || undefined}
            className="flex gap-1"
          >
            {[1, 2, 3, 4, 5].map((n) => (
              <RadioGroup.Item
                key={n}
                value={String(n)}
                aria-label={`${n} star${n > 1 ? 's' : ''}`}
                className="group grid size-12 place-items-center rounded-md transition duration-150 hover:bg-surface-interactive active:scale-98"
              >
                <Star className={cx('size-6', (ride.rating ?? 0) >= n ? 'fill-amber text-amber' : 'text-fg-secondary')} aria-hidden />
              </RadioGroup.Item>
            ))}
          </RadioGroup.Root>
          {rate.state.phase === 'error' && <p className="text-caption-sm text-coral-text">{rate.state.message}</p>}
        </fieldset>
      </div>
    </Panel>
  );
}

function Cancelled({ ride }: { ride: Ride }) {
  return (
    <Panel title="Ride cancelled" titleId="status-title" action={<Badge tone="coral">Cancelled</Badge>}>
      <div className="flex flex-col gap-3">
        <RouteLine ride={ride} />
        <p className="flex items-center gap-2 text-body-md text-fg-secondary">
          <Clock className="size-4" aria-hidden />
          {ride.cancelledBy === 'DRIVER' ? `Released by Jashim after waiting ${formatClock(NO_SHOW_WAIT_MS)}.` : `Reason: ${ride.cancelReason ?? 'not given'}.`}
        </p>
        <p className="text-caption-sm text-fg-secondary">No penalty was charged. Your pickup and drop-off are still filled in below.</p>
      </div>
    </Panel>
  );
}
