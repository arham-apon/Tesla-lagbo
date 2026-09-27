'use client';

import { BatteryCharging, Users } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';

import { CorridorMap, MapChip } from '@/components/map/CorridorMap';
import { Button } from '@/components/ui/Button';
import { InlineMessage } from '@/components/ui/primitives';
import { isLive, isTerminal, passengerCancelWindow } from '@/domain/lifecycle';
import type { DhakaZone, PassengerName, SeatIndex } from '@/types/mobility';
import { formatTakaSymbol } from '@/domain/money';
import { useNow } from '@/hooks/useNow';
import { occupiedSeatCount, openSeats } from '@/sim/selectors';
import { currentRide, planJoin, poolMembers, reserveSeatRace, type Ride, type World } from '@/sim/world';
import { useEngine, useWorld } from '@/state/EngineProvider';
import { useCommand } from '@/state/useCommand';
import { useViewer, type RouteDraft } from '@/state/viewer';
import { formatClock } from '@/utils/time';

import { CabinSeatManifest } from '@/components/dispatch/CabinSeatManifest';
import { CancelRideDialog } from './CancelRideDialog';
import { ContentionAlert, ContentionEngine } from './Contention';
import { FarePanel, useQuote } from './FarePanel';
import { PersonaSwitcher } from './PersonaSwitcher';
import { RideStatusCard } from './RideStatusCard';
import { RouteSelector } from './RouteSelector';
import { TripProgress } from './TripProgress';

const BLOCKER_ID = 'booking-blocker';
const REJECTION_HOLD_MS = 1600;

/** Why the commuter can't reserve a seat right now, in words (null = go ahead). */
export function bookingBlocker(w: World, ride: Ride | null, draft: RouteDraft): string | null {
  if (ride && isLive(ride.status)) return 'You already have a live ride.';
  if (ride && isTerminal(ride.status)) return 'Start a new booking to pick a seat.';
  if (draft.pickup === draft.dropoff) return 'Pickup and drop-off must be different zones.';
  if (w.pool?.departed) return 'Bullet has already left with its riders. Seats reopen when the pool finishes.';
  if (openSeats(w).length === 0) return 'Bullet is full (3 / 3).';
  if (w.pool) {
    const plan = planJoin(w, draft.pickup, draft.dropoff);
    if (!plan.ok) return plan.reason;
  }
  return null;
}

/** Dry-run the race on the pure model so the toggle only arms when a race can actually happen. */
function raceBlocker(w: World, persona: PassengerName, draft: RouteDraft, seat: SeatIndex | null): string | null {
  if (!seat) return 'No open seat to contend for.';
  const { world, result } = reserveSeatRace(w, { passenger: persona, seatIndex: seat, pickup: draft.pickup, dropoff: draft.dropoff }, 0);
  if (persona === 'Shirin') return !result.ok && result.error.code === 'SEAT_TAKEN' ? null : 'Nusrat has no way to claim this seat right now.';
  return result.ok && world.contention.Shirin ? null : 'Shirin already has a live ride, so nobody can race you for this seat.';
}

export function PassengerConsole() {
  const { persona } = useViewer();
  // Keyed by commuter: each phone has its own in-flight commands and error states.
  return <PassengerBooking key={persona} persona={persona} />;
}

function PassengerBooking({ persona }: { persona: PassengerName }) {
  const world = useWorld();
  const engine = useEngine();
  const { drafts, updateDraft, raceArmed, setRaceArmed } = useViewer();
  const ride = currentRide(world, persona);
  const status = ride?.status ?? 'IDLE';
  const live = !!ride && isLive(ride.status);
  const draft = drafts[persona];
  const pickup: DhakaZone = live ? ride.pickupZone : draft.pickup;
  const dropoff: DhakaZone = live ? ride.dropoffZone : draft.dropoff;

  const open = openSeats(world);
  const selectedSeat = draft.seat && open.includes(draft.seat) ? draft.seat : (open[0] ?? null);
  const blocker = bookingBlocker(world, ride, draft);
  const raceReason = blocker ?? raceBlocker(world, persona, draft, selectedSeat);
  const armed = raceArmed && !raceReason;

  const members = poolMembers(world);
  const poolKey = `${world.pool?.id ?? '-'}:${world.pool?.departed ?? false}:${members.map((m) => m.id).join(',')}`;
  const quote = useQuote(draft.pickup, draft.dropoff, !ride && draft.pickup !== draft.dropoff, poolKey);

  const reserve = useCommand((seat: SeatIndex, race: boolean) =>
    engine.reserveSeat({ passenger: persona, seatIndex: seat, pickup: draft.pickup, dropoff: draft.dropoff }, { simulateRace: race }),
  );
  const [rejectedAt, setRejectedAt] = useState<number | null>(null);
  useEffect(() => {
    if (rejectedAt === null) return;
    const t = setTimeout(() => setRejectedAt(null), REJECTION_HOLD_MS);
    return () => clearTimeout(t);
  }, [rejectedAt]);

  const onReserve = async () => {
    if (!selectedSeat || blocker) return;
    const result = await reserve.run(selectedSeat, armed);
    if (armed) setRaceArmed(false);
    if (result && !result.ok && result.error.code === 'SEAT_TAKEN') setRejectedAt(Date.now());
  };

  const contention = world.contention[persona];
  const showContention = !!contention && status === 'REQUESTED';

  return (
    <div className="flex flex-1 flex-col">
      <div className="mx-auto flex w-full max-w-screen-xl flex-1 flex-col gap-6 px-4 py-6 md:px-8">
        <PersonaSwitcher />
        <TripProgress status={status} />

        <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-12">
          <aside aria-label="Live map" className="hidden lg:col-span-5 lg:row-span-2 lg:block xl:col-span-4">
            <div className="sticky top-header">
              <CorridorMap
                world={world}
                overlay={
                  <>
                    <MapChip>
                      <Users className="size-4 text-mint" aria-hidden />
                      <span className="font-mono tabular-nums">{occupiedSeatCount(world)} / 3</span> seats
                    </MapChip>
                    <MapChip>
                      <BatteryCharging className="size-4 text-mint" aria-hidden />
                      <span className="font-mono tabular-nums">{world.vehicle.batteryPercent}%</span>
                    </MapChip>
                  </>
                }
              />
            </div>
          </aside>

          <div className="flex flex-col gap-6 lg:col-span-7 lg:col-start-6 xl:col-span-5 xl:col-start-5">
            {ride && <RideStatusCard world={world} ride={ride} />}
            {showContention && <ContentionAlert contention={contention} onDismiss={() => engine.dismissContention(persona)} />}
            <CabinSeatManifest
              world={world}
              persona={persona}
              selectedSeat={blocker ? null : selectedSeat}
              onSelect={(seat) => updateDraft(persona, { seat })}
              blocker={blocker}
              blockerId={BLOCKER_ID}
              securingSeat={reserve.pending ? selectedSeat : null}
              contention={rejectedAt !== null ? contention : null}
            />
            <RouteSelector
              world={world}
              pickup={pickup}
              dropoff={dropoff}
              locked={live}
              onChange={(route) => updateDraft(persona, route)}
            />
          </div>

          <div className="flex flex-col gap-6 lg:col-span-7 lg:col-start-6 xl:col-span-3 xl:col-start-10 xl:row-start-1">
            <FarePanel
              pickup={pickup}
              dropoff={dropoff}
              rideFare={ride?.fare ?? null}
              quoteState={quote.state}
              onRetry={quote.retry}
              settled={status === 'COMPLETED'}
            />
            <ContentionEngine persona={persona} armed={armed} onArmedChange={setRaceArmed} disabledReason={raceReason} />
          </div>
        </div>
      </div>

      <ActionDock
        persona={persona}
        ride={ride}
        seat={selectedSeat}
        blocker={blocker}
        fare={ride?.fare.netPoysha ?? (quote.state.phase === 'ready' ? quote.state.quote.fare.netPoysha : null)}
        reserve={reserve}
        onReserve={onReserve}
        rejected={rejectedAt !== null}
        onNewBooking={() => engine.startNewBooking(persona)}
      />
    </div>
  );
}

interface ActionDockProps {
  readonly persona: PassengerName;
  readonly ride: Ride | null;
  readonly seat: SeatIndex | null;
  readonly blocker: string | null;
  readonly fare: number | null;
  readonly reserve: ReturnType<typeof useCommand<[SeatIndex, boolean], unknown>>;
  readonly onReserve: () => void;
  readonly rejected: boolean;
  readonly onNewBooking: () => void;
}

/** Persistent bottom drawer: the one primary action for the current state, in thumb reach. */
function ActionDock({ persona, ride, seat, blocker, fare, reserve, onReserve, rejected, onNewBooking }: ActionDockProps) {
  const now = useNow();
  const status = ride?.status ?? 'IDLE';
  const booking = status === 'IDLE' || rejected;
  const error = reserve.state.phase === 'error' ? reserve.state : null;

  let summary: string;
  let action: ReactNode;

  if (booking) {
    summary = seat ? `Seat ${seat}${fare !== null ? ` · ${formatTakaSymbol(fare)}` : ''}` : 'No open seat';
    action = (
      <Button
        fullWidth
        className="sm:w-auto"
        onClick={() => void onReserve()}
        disabled={!!blocker && !rejected}
        aria-describedby={blocker ? BLOCKER_ID : undefined}
        loading={reserve.pending}
        loadingLabel="Securing Seat..."
        errorId={error ? 'reserve-error' : undefined}
        errorKey={error?.attempt}
      >
        {rejected ? `Seat ${error?.error.code === 'SEAT_TAKEN' ? error.error.seatIndex : ''} taken` : seat ? `Reserve Seat ${seat}` : 'Reserve Seat'}
      </Button>
    );
  } else if (status === 'COMPLETED' || status === 'CANCELLED') {
    summary = status === 'COMPLETED' ? 'Ride complete' : 'Ride cancelled';
    action = (
      <Button fullWidth className="sm:w-auto" onClick={onNewBooking}>
        {status === 'COMPLETED' ? 'Book another ride' : 'Back to booking'}
      </Button>
    );
  } else {
    const win = passengerCancelWindow(status, now, ride?.arrivedAt);
    summary = win.allowed
      ? win.remainingMs !== null
        ? `Free cancellation · ${formatClock(win.remainingMs)} left`
        : status === 'REQUESTED'
          ? 'Searching · cancel any time'
          : 'Matched · cancel without penalty'
      : win.reason;
    action = win.allowed ? (
      <CancelRideDialog
        passenger={persona}
        trigger={
          <Button variant="danger" fullWidth className="sm:w-auto">
            {status === 'REQUESTED' ? 'Cancel search' : 'Cancel ride'}
          </Button>
        }
      />
    ) : (
      <Button variant="danger" fullWidth className="sm:w-auto" disabled aria-describedby="dock-summary">
        Cancel ride
      </Button>
    );
  }

  return (
    <div className="sticky bottom-0 z-30 border-t border-line-subtle bg-surface-raised shadow-lifted">
      <div className="mx-auto flex w-full max-w-screen-xl flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between md:px-8">
        <div className="flex flex-col gap-1">
          <p id="dock-summary" className="font-mono text-telemetry-md tabular-nums text-fg-primary">
            {summary}
          </p>
          {error && error.error.code !== 'SEAT_TAKEN' && booking && <InlineMessage id="reserve-error">{error.message}</InlineMessage>}
          {error && error.error.code === 'SEAT_TAKEN' && rejected && (
            <InlineMessage id="reserve-error">Seat {error.error.seatIndex} was claimed {error.error.msAgo}ms before you.</InlineMessage>
          )}
        </div>
        {action}
      </div>
    </div>
  );
}
