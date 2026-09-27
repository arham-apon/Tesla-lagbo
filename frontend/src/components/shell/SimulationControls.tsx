'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { RotateCcw, SlidersHorizontal, X } from 'lucide-react';
import { useState } from 'react';

import { useAnnounce } from '@/components/a11y/Announcer';
import { Button } from '@/components/ui/Button';
import { SwitchField } from '@/components/ui/controls';
import { SCENARIOS } from '@/sim/scenarios';
import { useEngine } from '@/state/EngineProvider';
import { useViewer } from '@/state/viewer';

/** Demo harness: reset the PRD story and inject network failures. Not part of the rider/driver product. */
export function SimulationControls() {
  const engine = useEngine();
  const announce = useAnnounce();
  const { setRaceArmed } = useViewer();
  const [open, setOpen] = useState(false);
  const [flaky, setFlaky] = useState(engine.isFlaky);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <Button variant="secondary" aria-label="Simulation controls" icon={<SlidersHorizontal className="size-4" aria-hidden />}>
          <span className="hidden sm:inline">Simulation</span>
        </Button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas opacity-90" />
        <Dialog.Content className="fixed inset-x-4 top-16 z-50 mx-auto flex max-w-lg flex-col gap-6 rounded-xl border border-line-strong bg-surface-elevated p-6 shadow-lifted md:p-8">
          <div className="flex items-start justify-between gap-4">
            <div className="flex flex-col gap-1">
              <Dialog.Title className="text-title-xl text-fg-primary">Simulation controls</Dialog.Title>
              <Dialog.Description className="text-body-md text-fg-secondary">
                The consoles run against an in-browser dispatch server that follows the backend&apos;s pooling rules.
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <Button variant="ghost" aria-label="Close simulation controls" icon={<X className="size-4" aria-hidden />} />
            </Dialog.Close>
          </div>

          <fieldset className="flex flex-col gap-3">
            <legend className="mb-2 text-heading-lg text-fg-primary">Reset the story</legend>
            {SCENARIOS.map((s) => (
              <div key={s.id} className="flex flex-col gap-2 rounded-lg border border-line-subtle bg-surface-raised p-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex flex-col gap-1">
                  <span className="text-body-md font-semibold text-fg-primary">{s.label}</span>
                  <span className="text-caption-sm text-fg-secondary">{s.description}</span>
                </div>
                <Button
                  variant="secondary"
                  icon={<RotateCcw className="size-4" aria-hidden />}
                  onClick={() => {
                    engine.reset(s.id);
                    setRaceArmed(false);
                    announce(`Scenario reset: ${s.label}.`);
                    setOpen(false);
                  }}
                >
                  Load
                </Button>
              </div>
            ))}
          </fieldset>

          <SwitchField
            label="Flaky network"
            description="Every third request fails, to exercise error states and retries. Nothing changes on a failed request."
            checked={flaky}
            onCheckedChange={(v) => {
              engine.setFlaky(v);
              setFlaky(v);
              announce(v ? 'Flaky network on.' : 'Flaky network off.');
            }}
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
