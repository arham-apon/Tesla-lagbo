'use client';

import * as Accordion from '@radix-ui/react-accordion';
import { ChevronDown, RefreshCw, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/Button';
import { InlineMessage, Panel, Skeleton } from '@/components/ui/primitives';
import { DEMO_TARIFF, type FareBreakdown } from '@/domain/fare';
import type { DhakaZone } from '@/types/mobility';
import { formatPoysha, formatTaka, formatTakaSymbol, speakTaka } from '@/domain/money';
import { formatKm } from '@/domain/zones';
import type { Quote, Rejection } from '@/sim/world';
import { useEngine } from '@/state/EngineProvider';
import { describeRejection } from '@/state/messages';
import { cx } from '@/utils/cx';

type QuoteState =
  | { readonly phase: 'loading' }
  | { readonly phase: 'ready'; readonly quote: Quote }
  | { readonly phase: 'error'; readonly error: Rejection };

/** Price a draft route through the dispatch server. Refetches when the route or Bullet's pool changes. */
export function useQuote(pickup: DhakaZone, dropoff: DhakaZone, enabled: boolean, poolKey: string) {
  const engine = useEngine();
  const [nonce, setNonce] = useState(0);
  const key = `${pickup}|${dropoff}|${poolKey}|${nonce}`;
  const [settled, setSettled] = useState<{ key: string; state: QuoteState } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    void engine.quote(pickup, dropoff).then((r) => {
      if (live) setSettled({ key, state: r.ok ? { phase: 'ready', quote: r.value } : { phase: 'error', error: r.error } });
    });
    return () => {
      live = false;
    };
  }, [engine, pickup, dropoff, enabled, key]);
  // Loading is derived: the last answer belongs to a different question.
  const state: QuoteState = settled && settled.key === key ? settled.state : { phase: 'loading' };
  return { state, retry: () => setNonce((n) => n + 1) };
}

export interface FarePanelProps {
  readonly pickup: DhakaZone;
  readonly dropoff: DhakaZone;
  /** A live or finished ride's fare (server-authoritative); null while drafting. */
  readonly rideFare: FareBreakdown | null;
  readonly quoteState: QuoteState;
  readonly onRetry: () => void;
  readonly settled?: boolean;
}

/** Progressive disclosure: one clean total up front, the verified calculation one tap away. */
export function FarePanel({ pickup, dropoff, rideFare, quoteState, onRetry, settled }: FarePanelProps) {
  const fare = rideFare ?? (quoteState.phase === 'ready' ? quoteState.quote.fare : null);
  const loading = !rideFare && quoteState.phase === 'loading';
  const error = !rideFare && quoteState.phase === 'error' ? quoteState.error : null;

  const caption = !fare
    ? ' '
    : settled
      ? 'Final fare · settled with TeslaPay'
      : fare.pooled
        ? `Pool fare · ${fare.discountPct}% Dhaka Tesla Pool discount applied`
        : rideFare
          ? 'Solo fare · the pool discount applies when a co-rider joins'
          : 'Solo estimate · the pool discount applies when a co-rider joins';

  return (
    <Panel title={settled ? 'Final fare' : 'Fare'} titleId="fare-title" aria-busy={loading || undefined}>
      <div className="flex flex-col gap-2">
        {/* Fixed-height readout: skeleton, value and error all occupy the same box (no layout shift). */}
        <div className="flex h-8 items-center">
          {loading ? (
            <Skeleton className="h-6 w-1/2" />
          ) : fare ? (
            <p className="font-mono text-telemetry-lg tabular-nums text-fg-primary" aria-label={speakTaka(fare.netPoysha)}>
              {formatTakaSymbol(fare.netPoysha)}
            </p>
          ) : (
            <p className="font-mono text-telemetry-lg tabular-nums text-fg-secondary">৳ --.--</p>
          )}
        </div>
        {error ? (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <InlineMessage id="fare-error">{describeRejection(error)}</InlineMessage>
            <Button variant="secondary" onClick={onRetry} icon={<RefreshCw className="size-4" aria-hidden />} aria-describedby="fare-error">
              Retry quote
            </Button>
          </div>
        ) : (
          <p className="text-caption-sm text-fg-secondary">{loading ? 'Pricing your route...' : caption}</p>
        )}
      </div>

      <Accordion.Root type="single" collapsible className="mt-4">
        <Accordion.Item value="breakdown" className="rounded-lg border border-line-subtle">
          <Accordion.Header>
            <Accordion.Trigger
              disabled={!fare}
              className={cx(
                'group flex min-h-touch w-full items-center justify-between gap-2 rounded-lg px-4 py-3 text-left text-body-md font-semibold text-fg-primary',
                'transition duration-150 hover:bg-surface-interactive active:scale-98 disabled:cursor-not-allowed disabled:opacity-40',
              )}
            >
              <span className="inline-flex items-center gap-2">
                <ShieldCheck className="size-4 text-mint" aria-hidden />
                View Verified Fare Breakdown
              </span>
              <ChevronDown className="size-4 text-fg-secondary transition duration-300 group-data-open:rotate-180" aria-hidden />
            </Accordion.Trigger>
          </Accordion.Header>
          <Accordion.Content className="overflow-hidden data-closed:animate-accordion-up data-open:animate-accordion-down">
            {fare && <Breakdown fare={fare} pickup={pickup} dropoff={dropoff} />}
          </Accordion.Content>
        </Accordion.Item>
      </Accordion.Root>
    </Panel>
  );
}

function Breakdown({ fare, pickup, dropoff }: { fare: FareBreakdown; pickup: DhakaZone; dropoff: DhakaZone }) {
  const perSeat = fare.seats > 1 ? ` × ${fare.seats} seats` : '';
  const rows: { label: string; poysha: number; strong?: boolean; rule?: boolean }[] = [
    { label: `Base Corridor Rate${perSeat}`, poysha: fare.basePoysha },
    { label: `+ Distance Surcharge (${formatKm(fare.distanceM)} × ৳${formatTaka(DEMO_TARIFF.perKmPoysha)}/km)`, poysha: fare.distanceChargePoysha },
    { label: 'Subtotal Fare', poysha: fare.subtotalPoysha, rule: true },
    { label: `− Dhaka Tesla Pool Discount (${fare.discountPct}%)`, poysha: -fare.discountPoysha },
    { label: 'Final Net Charge', poysha: fare.netPoysha, strong: true, rule: true },
  ];
  return (
    <table className="w-full border-t border-line-subtle text-left">
      <caption className="px-4 pt-3 text-left text-caption-sm text-fg-secondary">
        Fare Calculation Breakdown ({pickup} → {dropoff} corridor)
      </caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Item</th>
          <th scope="col">Amount</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label} className={cx(r.rule && 'border-t border-line-subtle')}>
            <th scope="row" className={cx('py-2 pl-4 align-top text-body-md', r.strong ? 'font-semibold text-fg-primary' : 'font-normal text-fg-secondary')}>
              {r.label}
            </th>
            <td className="px-4 py-2 text-right align-top">
              <span className={cx('block font-mono text-telemetry-md tabular-nums', r.strong ? 'text-mint' : 'text-fg-primary')}>
                {formatTakaSymbol(r.poysha)}
              </span>
              <span className="block font-mono text-caption-sm tabular-nums text-fg-secondary">{formatPoysha(r.poysha)}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
