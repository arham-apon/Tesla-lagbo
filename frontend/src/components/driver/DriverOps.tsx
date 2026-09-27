'use client';

import * as Tabs from '@radix-ui/react-tabs';
import { BadgeCheck, CircleDashed, History, ReceiptText } from 'lucide-react';

import { Badge, Panel, Telemetry } from '@/components/ui/primitives';
import { STATUS_LABEL } from '@/domain/lifecycle';
import { formatTakaSymbol } from '@/domain/money';
import { earningsTotal, occupiedSeatCount, seatRide } from '@/sim/selectors';
import { SEAT_INDICES, type World } from '@/sim/world';
import { cx } from '@/utils/cx';
import { formatTimeOfDay } from '@/utils/time';

/** Seat allocation with destination verification chips (verified once the rider is on board). */
export function PassengerManifest({ world }: { world: World }) {
  const occupied = occupiedSeatCount(world);
  return (
    <Panel
      title="Passenger manifest"
      titleId="manifest-title"
      action={<span className="font-mono text-telemetry-md tabular-nums text-fg-primary">{occupied} / 3</span>}
    >
      <ul aria-labelledby="manifest-title" className="flex flex-col gap-3">
        {SEAT_INDICES.map((i) => {
          const ride = seatRide(world, i);
          if (!ride) {
            return (
              <li key={i} className="flex min-h-touch items-center gap-3 rounded-lg border border-dashed border-line-strong px-3 py-2">
                <span className="font-mono text-caption-sm text-fg-secondary">S{i}</span>
                <CircleDashed className="size-4 text-fg-secondary" aria-hidden />
                <span className="text-body-md text-fg-secondary">Open seat</span>
              </li>
            );
          }
          const held = ride.status === 'REQUESTED';
          const verified = ride.status === 'IN_TRANSIT';
          return (
            <li key={i} className={cx('flex flex-col gap-2 rounded-lg border px-3 py-2', held ? 'border-amber bg-amber-subtle' : 'border-line-subtle bg-surface-elevated')}>
              <div className="flex items-center gap-3">
                <span className="font-mono text-caption-sm text-fg-secondary">S{i}</span>
                <span className="text-heading-lg text-fg-primary">
                  {ride.passenger}
                  {ride.seats.length > 1 && ride.seats[0] !== i && <span className="text-caption-sm text-fg-secondary"> (companion)</span>}
                </span>
                <Badge tone={held ? 'amber' : ride.status === 'DRIVER_ARRIVED' ? 'mint' : 'neutral'} className="ml-auto">
                  {held ? 'Request pending' : STATUS_LABEL[ride.status]}
                </Badge>
              </div>
              <p className="text-caption-sm text-fg-secondary">
                {ride.pickupLandmark} → {ride.dropoffLandmark}
              </p>
              {!held && (
                <Badge tone={verified ? 'mint' : 'neutral'} icon={verified ? <BadgeCheck className="size-4" aria-hidden /> : undefined} className="self-start">
                  {verified ? `Destination verified: ${ride.dropoffZone}` : `Verify ${ride.dropoffZone} at pickup`}
                </Badge>
              )}
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}

/** Secondary operations stay behind tabs so they never compete with the road. */
export function OpsTabs({ world }: { world: World }) {
  const tabClass = cx(
    'inline-flex min-h-touch flex-1 items-center justify-center gap-2 rounded-md px-3 text-body-md font-semibold text-fg-secondary transition duration-150',
    'hover:bg-surface-interactive hover:text-fg-primary active:scale-98 data-active:bg-surface-elevated data-active:text-fg-primary',
  );
  return (
    <section aria-label="Secondary operations" className="rounded-xl border border-line-subtle bg-surface-raised p-4 shadow-ambient">
      <Tabs.Root defaultValue="ledger" className="flex flex-col gap-4">
        <Tabs.List aria-label="Secondary operations" className="flex gap-1 rounded-lg bg-canvas p-1">
          <Tabs.Trigger value="ledger" className={tabClass}>
            <ReceiptText className="size-4" aria-hidden />
            Ledger
          </Tabs.Trigger>
          <Tabs.Trigger value="history" className={tabClass}>
            <History className="size-4" aria-hidden />
            Trip history
          </Tabs.Trigger>
        </Tabs.List>

        <Tabs.Content value="ledger" className="flex flex-col gap-4">
          <dl>
            <Telemetry label="Settled today (poysha ledger)" size="lg" tone="mint" value={formatTakaSymbol(earningsTotal(world))} />
          </dl>
          {world.ledger.length === 0 ? (
            <p className="text-body-md text-fg-secondary">No settled fares yet. Each completed drop-off posts here.</p>
          ) : (
            <ul className="flex flex-col divide-y divide-line-subtle">
              {world.ledger.map((e) => (
                <li key={e.receiptId} className="flex items-center justify-between gap-3 py-2">
                  <div className="flex flex-col">
                    <span className="text-body-md text-fg-primary">
                      {e.passenger} · {e.route}
                    </span>
                    <span className="font-mono text-caption-sm tabular-nums text-fg-secondary">
                      {e.receiptId} · {formatTimeOfDay(e.settledAt)} {e.pooled ? '· pooled' : ''}
                    </span>
                  </div>
                  <span className="font-mono text-telemetry-md tabular-nums text-fg-primary">{formatTakaSymbol(e.netPoysha)}</span>
                </li>
              ))}
            </ul>
          )}
        </Tabs.Content>

        <Tabs.Content value="history" className="flex flex-col gap-3">
          {world.history.length === 0 ? (
            <p className="text-body-md text-fg-secondary">Finished pools are archived here.</p>
          ) : (
            world.history.map((t) => (
              <article key={t.id} className="flex flex-col gap-2 rounded-lg border border-line-subtle bg-surface-elevated p-3">
                <header className="flex items-center justify-between gap-2">
                  <h3 className="font-mono text-telemetry-md text-fg-primary">{t.id}</h3>
                  <span className="font-mono text-caption-sm tabular-nums text-fg-secondary">
                    {formatTimeOfDay(t.openedAt)}–{formatTimeOfDay(t.closedAt)}
                  </span>
                </header>
                <ul className="flex flex-col gap-1">
                  {t.riders.map((r) => (
                    <li key={`${t.id}-${r.passenger}-${r.route}`} className="flex items-center justify-between gap-2 text-body-md">
                      <span className={r.status === 'CANCELLED' ? 'text-fg-secondary' : 'text-fg-primary'}>
                        {r.passenger} · {r.route}
                      </span>
                      <span className="font-mono text-caption-sm tabular-nums text-fg-secondary">
                        {r.netPoysha !== null ? formatTakaSymbol(r.netPoysha) : 'cancelled'}
                      </span>
                    </li>
                  ))}
                </ul>
                <p className="text-caption-sm text-mint">Earned {formatTakaSymbol(t.earningsPoysha)}</p>
              </article>
            ))
          )}
        </Tabs.Content>
      </Tabs.Root>
    </section>
  );
}
