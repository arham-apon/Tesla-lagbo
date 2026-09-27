import { Check } from 'lucide-react';

import { PROGRESS_STEPS, progressIndex } from '@/domain/lifecycle';
import type { RideLifecycleStatus } from '@/types/mobility';
import { cx } from '@/utils/cx';

/** Requested → Matched → In Transit → Completed, as an ordered list with aria-current="step". */
export function TripProgress({ status }: { status: RideLifecycleStatus }) {
  const current = progressIndex(status);
  const cancelled = status === 'CANCELLED';
  return (
    <nav aria-label="Trip progress">
      <ol className="grid grid-cols-4 gap-2">
        {PROGRESS_STEPS.map((step, i) => {
          const done = !cancelled && i < current;
          const active = !cancelled && i === current;
          const finished = active && status === 'COMPLETED';
          return (
            <li key={step.status} aria-current={active ? 'step' : undefined} className="flex flex-col gap-2">
              <span
                aria-hidden
                className={cx(
                  'h-1 rounded-full transition duration-300',
                  cancelled ? 'bg-coral' : done || finished ? 'bg-mint' : active ? 'bg-amber' : 'bg-surface-interactive',
                )}
              />
              <span
                className={cx(
                  'inline-flex items-center gap-1 text-caption-sm',
                  active ? 'text-fg-primary' : done ? 'text-mint' : 'text-fg-secondary',
                )}
              >
                {(done || finished) && <Check className="size-4" aria-hidden />}
                {step.label}
                {done && <span className="sr-only">(done)</span>}
              </span>
            </li>
          );
        })}
      </ol>
      {cancelled && <p className="mt-2 text-caption-sm text-coral-text">This ride was cancelled.</p>}
    </nav>
  );
}
