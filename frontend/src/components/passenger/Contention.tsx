'use client';

import { motion } from 'framer-motion';
import { ShieldAlert, X } from 'lucide-react';

import { SwitchField } from '@/components/ui/controls';
import { Panel } from '@/components/ui/primitives';
import type { PassengerName } from '@/types/mobility';
import { CONTENTION_GAP_MS, type Contention } from '@/sim/world';

/**
 * The high-visibility lock-conflict banner. role="alert" makes it an assertive live region: it is spoken
 * the moment it appears, without a page refresh or a modal stealing focus.
 */
export function ContentionAlert({ contention, onDismiss }: { contention: Contention; onDismiss: () => void }) {
  return (
    <motion.div
      role="alert"
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex items-start gap-3 rounded-xl border-2 border-coral bg-coral-subtle py-3 pl-4 pr-1 shadow-lifted"
    >
      <ShieldAlert className="mt-1 size-6 shrink-0 text-coral-text" aria-hidden />
      <div className="flex flex-1 flex-col gap-1 py-1">
        <p className="text-body-md font-bold text-fg-primary">Lock conflict handled</p>
        <p className="text-body-md text-fg-primary">
          Seat {contention.seatIndex} was claimed by another commuter {contention.msAgo}ms ago. Searching for the next available pooled Tesla...
        </p>
        <p className="text-caption-sm text-fg-secondary">
          Winning transaction <span className="font-mono">{contention.winnerTx}</span>. Your pickup and drop-off are kept.
        </p>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss seat contention alert"
        className="grid size-12 shrink-0 place-items-center rounded-md text-coral-text transition duration-150 hover:bg-coral-subtle active:scale-98"
      >
        <X className="size-4" aria-hidden />
      </button>
    </motion.div>
  );
}

export function ContentionEngine({
  persona,
  armed,
  onArmedChange,
  disabledReason,
}: {
  persona: PassengerName;
  armed: boolean;
  onArmedChange: (armed: boolean) => void;
  disabledReason: string | null;
}) {
  const description =
    persona === 'Shirin'
      ? `On your next reservation, Nusrat's claim for the same seat reaches the server ${CONTENTION_GAP_MS}ms earlier.`
      : `On your next reservation, Shirin claims the same seat ${CONTENTION_GAP_MS}ms after you.`;
  return (
    <Panel title="Concurrency contention engine" titleId="contention-title">
      <SwitchField
        label="Simulate Shirin Concurrent Booking"
        description={description}
        checked={armed}
        onCheckedChange={onArmedChange}
        disabled={!!disabledReason}
        disabledReason={disabledReason ?? undefined}
      />
    </Panel>
  );
}
