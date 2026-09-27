'use client';

import { Check, Radar, WifiOff } from 'lucide-react';
import type { ReactNode } from 'react';

import { Button } from '@/components/ui/Button';
import { SlideToConfirm } from '@/components/ui/controls';
import { CLOCK_PLACEHOLDER, InlineMessage, Panel, Telemetry } from '@/components/ui/primitives';
import { useNow } from '@/hooks/useNow';
import { driverStops, etaFor, NO_SHOW_WAIT_MS, type DriverStop, type World } from '@/sim/world';
import { useEngine } from '@/state/EngineProvider';
import { useCommand } from '@/state/useCommand';
import { cx } from '@/utils/cx';
import { formatClock } from '@/utils/time';

/** The one thing to do next, in large type, with a 56 px control. */
export function NextStop({ world }: { world: World }) {
  const stops = driverStops(world);
  const current = stops.find((s) => s.state === 'current');

  if (!world.vehicle.online) {
    return (
      <Panel title="You're offline" titleId="next-stop-title">
        <p className="flex items-center gap-2 text-body-md text-fg-secondary">
          <WifiOff className="size-4" aria-hidden />
          Switch online to receive ride requests on the Banani corridor.
        </p>
      </Panel>
    );
  }
  if (!current) {
    return (
      <Panel title="Waiting for ride requests" titleId="next-stop-title">
        <p className="flex items-center gap-3 text-body-md text-fg-secondary">
          <Radar className="size-6 shrink-0 text-mint motion-safe:animate-pulse" aria-hidden />
          Bullet is waiting at {world.vehicle.currentZone}. New requests appear above with a 15-second response window.
        </p>
      </Panel>
    );
  }

  return (
    <section aria-labelledby="next-stop-title" className="flex flex-col gap-4 rounded-xl border-2 border-mint bg-surface-raised p-4 shadow-ambient md:p-6">
      <p className="text-caption-sm text-fg-secondary">
        Stop {current.seq} of {stops.length} · {current.kind === 'PICKUP' ? 'Pickup' : 'Drop-off'}
      </p>
      <h2 id="next-stop-title" className="text-display-2xl text-fg-primary">
        Next Stop: {current.landmark}
      </h2>
      <p className="text-heading-lg text-fg-secondary">
        {current.kind === 'PICKUP' ? `Pick up ${current.passenger}` : `Drop off ${current.passenger}`} · {current.zone}
      </p>
      <StopClock world={world} stop={current} />
      <StopActions key={`${current.id}-${current.rideStatus}`} world={world} stop={current} />
    </section>
  );
}

function StopClock({ world, stop }: { world: World; stop: DriverStop }) {
  const now = useNow();
  const ride = world.rides[stop.rideId]!;
  if (stop.rideStatus === 'DRIVER_ARRIVED') {
    const waited = now !== null && ride.arrivedAt !== undefined ? now - ride.arrivedAt : null;
    return (
      <dl>
        <Telemetry label="Arrival wait" size="lg" tone={waited !== null && waited >= NO_SHOW_WAIT_MS ? 'amber' : 'primary'} value={waited === null ? CLOCK_PLACEHOLDER : formatClock(waited)} />
      </dl>
    );
  }
  const eta = etaFor(world, ride);
  const remaining = now !== null && eta !== null ? eta - now : null;
  return (
    <dl>
      <Telemetry
        label={stop.kind === 'PICKUP' ? 'ETA to pickup' : 'ETA to drop-off'}
        size="lg"
        tone={world.trafficDelaySec && stop.kind === 'DROPOFF' ? 'amber' : 'mint'}
        value={remaining === null ? CLOCK_PLACEHOLDER : remaining > 0 ? formatClock(remaining) : 'Now'}
      />
    </dl>
  );
}

function StopActions({ world, stop }: { world: World; stop: DriverStop }) {
  const engine = useEngine();
  const now = useNow();
  const arrive = useCommand(() => engine.confirmArrival(stop.rideId));
  const board = useCommand(() => engine.confirmBoarding(stop.rideId));
  const complete = useCommand(() => engine.completeStop(stop.rideId));
  const noShow = useCommand(() => engine.markNoShow(stop.rideId));
  const errorId = `stop-error-${stop.seq}`;

  if (stop.kind === 'PICKUP' && stop.rideStatus === 'MATCHED') {
    return (
      <Action errorId={errorId} message={arrive.state.phase === 'error' ? arrive.state.message : null}>
        <Button
          size="cockpit"
          fullWidth
          onClick={() => void arrive.run()}
          loading={arrive.pending}
          loadingLabel="Confirming arrival..."
          errorId={arrive.state.phase === 'error' ? errorId : undefined}
          errorKey={arrive.state.phase === 'error' ? arrive.state.attempt : undefined}
        >
          Confirm Arrival
        </Button>
      </Action>
    );
  }

  if (stop.kind === 'PICKUP' && stop.rideStatus === 'DRIVER_ARRIVED') {
    const ride = world.rides[stop.rideId]!;
    const waited = now !== null && ride.arrivedAt !== undefined ? now - ride.arrivedAt : 0;
    const noShowReady = waited >= NO_SHOW_WAIT_MS;
    const message = board.state.phase === 'error' ? board.state.message : noShow.state.phase === 'error' ? noShow.state.message : null;
    return (
      <Action errorId={errorId} message={message}>
        <SlideToConfirm
          label="Confirm Passenger Boarding"
          onConfirm={() => void board.run()}
          loading={board.pending}
          loadingLabel="Starting trip..."
          errorId={message ? errorId : undefined}
        />
        <p className="text-caption-sm text-fg-secondary">Starts the trip for {stop.passenger}.</p>
        <Button
          variant="ghost"
          fullWidth
          onClick={() => void noShow.run()}
          disabled={!noShowReady}
          aria-describedby={!noShowReady ? `noshow-wait-${stop.seq}` : undefined}
          loading={noShow.pending}
          loadingLabel="Releasing seat..."
        >
          Passenger no-show
        </Button>
        {!noShowReady && (
          <InlineMessage id={`noshow-wait-${stop.seq}`} tone="neutral">
            Available after a {formatClock(NO_SHOW_WAIT_MS)} wait ({formatClock(NO_SHOW_WAIT_MS - waited)} left).
          </InlineMessage>
        )}
      </Action>
    );
  }

  return (
    <Action errorId={errorId} message={complete.state.phase === 'error' ? complete.state.message : null}>
      <Button
        size="cockpit"
        fullWidth
        onClick={() => void complete.run()}
        loading={complete.pending}
        loadingLabel="Settling fare..."
        errorId={complete.state.phase === 'error' ? errorId : undefined}
        errorKey={complete.state.phase === 'error' ? complete.state.attempt : undefined}
      >
        {stop.isFinal ? 'Complete Ride' : 'Complete Stop'}
      </Button>
    </Action>
  );
}

function Action({ children, errorId, message }: { children: ReactNode; errorId: string; message: string | null }) {
  return (
    <div className="flex flex-col gap-3">
      {children}
      {message && <InlineMessage id={errorId}>{message}</InlineMessage>}
    </div>
  );
}

type ChipState = 'done' | 'next' | 'pending';

function chipsFor(stop: DriverStop): { label: string; state: ChipState }[] {
  const s = stop.rideStatus;
  if (stop.kind === 'PICKUP') {
    const arrived = s === 'DRIVER_ARRIVED' || s === 'IN_TRANSIT' || s === 'COMPLETED';
    const started = s === 'IN_TRANSIT' || s === 'COMPLETED';
    return [
      { label: 'Confirm Arrival', state: arrived ? 'done' : stop.state === 'current' ? 'next' : 'pending' },
      { label: 'Start Trip', state: started ? 'done' : arrived && stop.state === 'current' ? 'next' : 'pending' },
    ];
  }
  return [{ label: stop.isFinal ? 'Complete Ride' : 'Complete Stop', state: s === 'COMPLETED' ? 'done' : stop.state === 'current' ? 'next' : 'pending' }];
}

const chipStyle: Record<ChipState, string> = {
  done: 'border-mint bg-mint-subtle text-mint',
  next: 'border-amber bg-amber-subtle text-amber',
  pending: 'border-line-subtle text-fg-secondary',
};

/** Sequential checkpoint navigation: every stop, in order, with its action chain. */
export function StopPipeline({ world }: { world: World }) {
  const stops = driverStops(world);
  if (!stops.length) return null;
  return (
    <Panel title="Dispatch queue" titleId="pipeline-title">
      <ol aria-labelledby="pipeline-title" className="flex flex-col">
        {stops.map((s, i) => (
          <li key={s.id} aria-current={s.state === 'current' ? 'step' : undefined} className="flex gap-3">
            <span className="flex flex-col items-center" aria-hidden>
              <span
                className={cx(
                  'grid size-6 shrink-0 place-items-center rounded-full border-2 font-mono text-caption-sm',
                  s.state === 'done' ? 'border-mint bg-mint text-fg-on-accent' : s.state === 'current' ? 'border-amber text-amber' : 'border-line-strong text-fg-secondary',
                )}
              >
                {s.state === 'done' ? <Check className="size-4" /> : s.seq}
              </span>
              {i < stops.length - 1 && <span className={cx('w-px flex-1', s.state === 'done' ? 'bg-mint' : 'bg-line-strong')} />}
            </span>
            <div className="flex flex-1 flex-col gap-2 pb-4">
              <p className={cx('text-body-md', s.state === 'done' ? 'text-fg-secondary' : 'text-fg-primary')}>
                <span className="font-semibold">Stop {s.seq}:</span> {s.kind === 'PICKUP' ? 'Pickup' : 'Drop-off'} {s.passenger} at {s.landmark}
                {s.state === 'done' && <span className="sr-only"> (done)</span>}
              </p>
              <ul aria-label={`Actions for stop ${s.seq}`} className="flex flex-wrap items-center gap-2">
                {chipsFor(s).map((c, ci) => (
                  <li key={c.label} className="flex items-center gap-2">
                    {ci > 0 && <span aria-hidden className="text-fg-secondary">→</span>}
                    <span className={cx('inline-flex items-center gap-1 rounded-sm border px-2 py-1 text-caption-sm', chipStyle[c.state])}>
                      {c.state === 'done' && <Check className="size-4" aria-hidden />}
                      {c.label}
                      <span className="sr-only">{c.state === 'done' ? ' (done)' : c.state === 'next' ? ' (next)' : ''}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </li>
        ))}
      </ol>
    </Panel>
  );
}
