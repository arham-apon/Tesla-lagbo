'use client';

import * as Dialog from '@radix-ui/react-dialog';
import * as RadioGroup from '@radix-ui/react-radio-group';
import { useState, type ReactNode } from 'react';

import { Button } from '@/components/ui/Button';
import { InlineMessage } from '@/components/ui/primitives';
import type { PassengerName } from '@/types/mobility';
import { useEngine } from '@/state/EngineProvider';
import { useCommand } from '@/state/useCommand';
import { cx } from '@/utils/cx';

const REASONS = ['Changed plans', 'Driver is too far away', 'Booked by mistake', 'Found another ride'] as const;

/** CANCELLED always asks why, then returns cleanly to the booking view. */
export function CancelRideDialog({ passenger, trigger }: { passenger: PassengerName; trigger: ReactNode }) {
  const engine = useEngine();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<string>(REASONS[0]);
  const cancel = useCommand(() => engine.cancelRide(passenger, reason));

  const confirm = async () => {
    const r = await cancel.run();
    if (r?.ok) setOpen(false);
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!cancel.pending) setOpen(v);
        if (v) cancel.reset();
      }}
    >
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas opacity-90" />
        <Dialog.Content className="fixed inset-x-4 bottom-4 z-50 mx-auto flex max-w-md flex-col gap-6 rounded-xl border border-line-strong bg-surface-elevated p-6 shadow-lifted sm:bottom-auto sm:top-16">
          <div className="flex flex-col gap-1">
            <Dialog.Title className="text-title-xl text-fg-primary">Cancel this ride?</Dialog.Title>
            <Dialog.Description className="text-body-md text-fg-secondary">
              No penalty is charged. Tell us why so Jashim&apos;s queue stays accurate.
            </Dialog.Description>
          </div>
          <RadioGroup.Root value={reason} onValueChange={setReason} aria-label="Cancellation reason" className="flex flex-col gap-2">
            {REASONS.map((r) => (
              <RadioGroup.Item
                key={r}
                value={r}
                className={cx(
                  'flex min-h-touch items-center gap-3 rounded-lg border-2 border-line-subtle bg-surface-raised px-4 text-left text-body-md text-fg-primary',
                  'transition duration-150 hover:bg-surface-interactive active:scale-98 data-checked:border-mint data-checked:bg-mint-subtle',
                )}
              >
                <span aria-hidden className="grid size-4 place-items-center rounded-full border-2 border-line-strong">
                  <RadioGroup.Indicator className="size-2 rounded-full bg-mint" />
                </span>
                {r}
              </RadioGroup.Item>
            ))}
          </RadioGroup.Root>
          {cancel.state.phase === 'error' && <InlineMessage id="cancel-error">{cancel.state.message}</InlineMessage>}
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Dialog.Close asChild>
              <Button variant="secondary" disabled={cancel.pending}>
                Keep ride
              </Button>
            </Dialog.Close>
            <Button
              variant="danger"
              onClick={() => void confirm()}
              loading={cancel.pending}
              loadingLabel="Cancelling..."
              errorId={cancel.state.phase === 'error' ? 'cancel-error' : undefined}
              errorKey={cancel.state.phase === 'error' ? cancel.state.attempt : undefined}
            >
              Cancel ride
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
