'use client';

import * as RadioGroup from '@radix-ui/react-radio-group';

import { PASSENGER_NAMES, PASSENGERS } from '@/domain/cast';
import { STATUS_LABEL } from '@/domain/lifecycle';
import type { PassengerName } from '@/types/mobility';
import { passengerStatus } from '@/sim/selectors';
import { useWorld } from '@/state/EngineProvider';
import { useViewer } from '@/state/viewer';
import { cx } from '@/utils/cx';

/** Whose phone are we looking at? Each commuter keeps their own ride and booking inputs. */
export function PersonaSwitcher() {
  const world = useWorld();
  const { persona, setPersona } = useViewer();
  return (
    <div className="flex flex-col gap-2">
      <span id="persona-label" className="text-caption-sm text-fg-secondary">
        Viewing as commuter
      </span>
      <RadioGroup.Root
        aria-labelledby="persona-label"
        value={persona}
        onValueChange={(v) => setPersona(v as PassengerName)}
        orientation="horizontal"
        className="grid grid-cols-3 gap-2"
      >
        {PASSENGER_NAMES.map((name) => {
          const status = passengerStatus(world, name);
          const contended = !!world.contention[name];
          return (
            <RadioGroup.Item
              key={name}
              value={name}
              className={cx(
                'flex min-h-touch flex-col items-start gap-1 rounded-lg border-2 px-3 py-2 text-left transition duration-150',
                'border-line-subtle bg-surface-raised hover:bg-surface-interactive hover:brightness-108 active:scale-98',
                'data-checked:border-mint data-checked:bg-mint-subtle',
              )}
            >
              <span className="text-body-md font-semibold text-fg-primary">{name}</span>
              <span className="hidden text-caption-sm text-fg-secondary sm:block">{PASSENGERS[name].role}</span>
              <span className={cx('text-caption-sm', contended ? 'text-coral-text' : status === 'IDLE' ? 'text-fg-secondary' : 'text-mint')}>
                {contended ? 'Seat conflict' : STATUS_LABEL[status]}
              </span>
            </RadioGroup.Item>
          );
        })}
      </RadioGroup.Root>
    </div>
  );
}
