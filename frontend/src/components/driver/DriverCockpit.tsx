'use client';

import * as RadixSwitch from '@radix-ui/react-switch';
import { motion } from 'framer-motion';
import { CarFront, LoaderCircle, MapPin, Navigation } from 'lucide-react';

import { CorridorMap, MapChip } from '@/components/map/CorridorMap';
import { Button } from '@/components/ui/Button';
import { BatteryGauge, CountdownRing, SeatPips } from '@/components/ui/controls';
import { InlineMessage, Telemetry } from '@/components/ui/primitives';
import { spring } from '@/design/tokens';
import { BULLET } from '@/domain/cast';
import { formatTakaSymbol } from '@/domain/money';
import { formatKm } from '@/domain/zones';
import { useNow } from '@/hooks/useNow';
import { occupiedSeatCount } from '@/sim/selectors';
import { driverStops, OFFER_WINDOW_MS, type Offer, type World } from '@/sim/world';
import { useEngine, useWorld } from '@/state/EngineProvider';
import { useCommand } from '@/state/useCommand';
import { cx } from '@/utils/cx';

import { OpsTabs, PassengerManifest } from './DriverOps';
import { NextStop, StopPipeline } from './DriverStops';

export function DriverCockpit() {
  const world = useWorld();
  const next = driverStops(world).find((s) => s.state === 'current');
  return (
    <div className="mx-auto flex w-full max-w-screen-xl flex-1 flex-col gap-6 px-4 py-6 md:px-8">
      <OperationalStatusBar world={world} />
      {world.offer && <OfferCard key={`${world.offer.rideId}-${world.offer.offeredAt}`} world={world} offer={world.offer} />}

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-12">
        <aside aria-label="Live map" className="hidden lg:col-span-5 lg:row-span-2 lg:block xl:col-span-4">
          <div className="sticky top-header">
            <CorridorMap
              world={world}
              overlay={
                next ? (
                  <MapChip>
                    <Navigation className="size-4 text-mint" aria-hidden />
                    Next: {next.landmark}
                  </MapChip>
                ) : (
                  <MapChip>
                    <MapPin className="size-4 text-fg-secondary" aria-hidden />
                    Waiting at {world.vehicle.currentZone}
                  </MapChip>
                )
              }
            />
          </div>
        </aside>
        <div className="flex flex-col gap-6 lg:col-span-7 lg:col-start-6 xl:col-span-5 xl:col-start-5">
          <NextStop world={world} />
          <StopPipeline world={world} />
        </div>
        <div className="flex flex-col gap-6 lg:col-span-7 lg:col-start-6 xl:col-span-3 xl:col-start-10 xl:row-start-1">
          <PassengerManifest world={world} />
          <OpsTabs world={world} />
        </div>
      </div>
    </div>
  );
}

/** Online switch, battery telemetry and live cabin load: everything Jashim glances at. */
function OperationalStatusBar({ world }: { world: World }) {
  const engine = useEngine();
  const toggle = useCommand((online: boolean) => engine.setOnline(online));
  const online = world.vehicle.online;
  const occupied = occupiedSeatCount(world);
  const error = toggle.state.phase === 'error' ? toggle.state : null;

  return (
    <section aria-label="Operational status" className="grid gap-4 rounded-xl border border-line-subtle bg-surface-raised p-4 shadow-ambient md:grid-cols-12 md:items-center">
      <div className="flex flex-col gap-2 md:col-span-5">
        <motion.div animate={error ? { x: [0, -6, 6, -6, 6, 0] } : { x: 0 }} key={error?.attempt ?? 0} transition={{ duration: 0.3 }}>
          <RadixSwitch.Root
            checked={online}
            onCheckedChange={(v) => void toggle.run(v)}
            aria-busy={toggle.pending || undefined}
            aria-invalid={!!error || undefined}
            aria-describedby={error ? 'online-error' : 'online-status'}
            className={cx(
              'group flex min-h-touch-cockpit w-full items-center justify-between gap-4 rounded-xl border-2 px-4 py-2 text-left transition duration-150',
              'border-line-strong bg-surface-elevated hover:brightness-108 active:scale-98 data-checked:border-mint data-checked:bg-mint-subtle',
              'aria-busy:cursor-progress',
              error && 'border-coral',
            )}
          >
            <span className="flex flex-col">
              <span className="text-heading-lg text-fg-primary">Online</span>
              <span id="online-status" className={cx('text-caption-sm', online ? 'text-mint' : 'text-fg-secondary')}>
                {toggle.pending ? 'Updating...' : online ? 'Receiving ride requests' : 'Offline · not receiving requests'}
              </span>
            </span>
            <span className="flex h-8 w-16 shrink-0 items-center justify-start rounded-full border-2 border-line-strong bg-surface-interactive p-1 group-data-checked:justify-end group-data-checked:border-mint group-data-checked:bg-mint-subtle">
              <RadixSwitch.Thumb asChild>
                <motion.span layout transition={spring} className="grid size-6 place-items-center rounded-full bg-fg-primary group-data-checked:bg-mint">
                  {toggle.pending && <LoaderCircle className="size-4 text-canvas motion-safe:animate-spin" aria-hidden />}
                </motion.span>
              </RadixSwitch.Thumb>
            </span>
          </RadixSwitch.Root>
        </motion.div>
        {error && <InlineMessage id="online-error">{error.message}</InlineMessage>}
      </div>

      <dl className="grid grid-cols-3 gap-4 md:col-span-7">
        <Telemetry
          label="Battery"
          value={
            <span className="inline-flex items-center gap-2">
              <BatteryGauge percent={world.vehicle.batteryPercent} />
              {world.vehicle.batteryPercent}%
            </span>
          }
          srValue={`${world.vehicle.batteryPercent} percent`}
        />
        <Telemetry
          label="Cabin capacity"
          value={
            <span className="inline-flex items-center gap-2">
              <SeatPips occupied={occupied} capacity={BULLET.totalSeatCapacity} />
              {occupied} / 3
            </span>
          }
          srValue={`${occupied} of 3 seats occupied`}
        />
        <Telemetry label={`${BULLET.vehicleIdentifier} at`} value={world.vehicle.currentZone} />
      </dl>
      <p className="sr-only" aria-live="polite">
        {occupied} / 3 Seats Occupied
      </p>
    </section>
  );
}

/** REQUESTED on the driver side: a new opportunity with a 15-second response countdown. */
function OfferCard({ world, offer }: { world: World; offer: Offer }) {
  const engine = useEngine();
  const now = useNow(250);
  const ride = world.rides[offer.rideId];
  const accept = useCommand(() => engine.acceptOffer());
  const decline = useCommand(() => engine.declineOffer());
  if (!ride) return null;
  const remaining = now === null ? OFFER_WINDOW_MS : offer.expiresAt - now;
  const busy = accept.pending || decline.pending;
  const error = accept.state.phase === 'error' ? accept.state : decline.state.phase === 'error' ? decline.state : null;

  return (
    <motion.section
      aria-labelledby="offer-title"
      initial={{ opacity: 0, y: -16 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col gap-4 rounded-xl border-2 border-amber bg-surface-elevated p-4 shadow-lifted md:p-6"
    >
      <div className="flex items-start gap-4">
        <CountdownRing remainingMs={remaining} totalMs={OFFER_WINDOW_MS} label="Time to respond" />
        <div className="flex flex-1 flex-col gap-1">
          <p className="text-caption-sm text-amber">New ride request</p>
          <h2 id="offer-title" className="text-title-xl text-fg-primary">
            {ride.passenger} · {ride.pickupLandmark}
          </h2>
          <p className="flex items-center gap-2 text-body-md text-fg-secondary">
            <CarFront className="size-4" aria-hidden />
            To {ride.dropoffLandmark} · {formatKm(ride.soloDistanceM)} · {ride.seats.length || 1} seat
          </p>
        </div>
        <Telemetry label="Est. fare" value={formatTakaSymbol(ride.fare.netPoysha)} className="text-right" />
      </div>
      {error && <InlineMessage id="offer-error">{error.message}</InlineMessage>}
      <div className="grid grid-cols-2 gap-3">
        <Button variant="secondary" size="cockpit" onClick={() => void decline.run()} loading={decline.pending} loadingLabel="Declining..." disabled={accept.pending}>
          Decline
        </Button>
        <Button
          size="cockpit"
          onClick={() => void accept.run()}
          loading={accept.pending}
          loadingLabel="Accepting..."
          disabled={decline.pending || (busy && !accept.pending)}
          errorId={accept.state.phase === 'error' ? 'offer-error' : undefined}
          errorKey={accept.state.phase === 'error' ? accept.state.attempt : undefined}
        >
          Accept
        </Button>
      </div>
    </motion.section>
  );
}
