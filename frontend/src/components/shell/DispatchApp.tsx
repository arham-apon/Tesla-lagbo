'use client';

import * as Tabs from '@radix-ui/react-tabs';
import { motion, MotionConfig } from 'framer-motion';
import { Gauge, UserRound, Zap } from 'lucide-react';
import { useState } from 'react';

import { AnnouncerProvider } from '@/components/a11y/Announcer';
import { DriverCockpit } from '@/components/driver/DriverCockpit';
import { PassengerConsole } from '@/components/passenger/PassengerConsole';
import { spring } from '@/design/tokens';
import type { ConsoleView } from '@/types/mobility';
import type { DispatchEngine } from '@/sim/engine';
import { EngineProvider } from '@/state/EngineProvider';
import { useViewer, ViewerProvider } from '@/state/viewer';
import { cx } from '@/utils/cx';

import { Notices } from './Notices';
import { SimulationControls } from './SimulationControls';

export function DispatchApp({ engine }: { engine?: DispatchEngine }) {
  return (
    <MotionConfig reducedMotion="user" transition={spring}>
      <EngineProvider engine={engine}>
        <AnnouncerProvider>
          <ViewerProvider>
            <Shell />
          </ViewerProvider>
        </AnnouncerProvider>
      </EngineProvider>
    </MotionConfig>
  );
}

const VIEWS: readonly { value: ConsoleView; label: string; short: string; icon: typeof Gauge }[] = [
  { value: 'passenger', label: 'Passenger Console', short: 'Passenger', icon: UserRound },
  { value: 'driver', label: 'Driver Cockpit', short: 'Driver', icon: Gauge },
];

function Shell() {
  const { view, setView } = useViewer();
  // First paint is fully visible (no JS needed); only later console switches slide in.
  const [switched, setSwitched] = useState(false);
  return (
    <Tabs.Root
      value={view}
      onValueChange={(v) => {
        setSwitched(true);
        setView(v as ConsoleView);
      }}
      className="flex min-h-screen flex-col"
    >
      <a
        href="#console"
        className="sr-only z-50 rounded-md bg-mint px-4 py-3 text-fg-on-accent focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
      >
        Skip to console
      </a>
      <header className="sticky top-0 z-40 border-b border-line-subtle bg-surface-raised">
        <div className="mx-auto flex max-w-screen-xl flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3 md:px-8">
          <div className="flex min-w-0 flex-1 items-center gap-3 md:flex-none">
            <span className="grid size-12 shrink-0 place-items-center rounded-lg bg-mint-subtle text-mint" aria-hidden>
              <Zap className="size-6" />
            </span>
            <div className="flex flex-col">
              <span className="text-title-xl text-fg-primary">Dhaka Tesla Pool</span>
              <span className="text-caption-sm text-fg-secondary">Banani · Gulshan · Mohakhali corridor</span>
            </div>
          </div>
          <Tabs.List aria-label="Choose console" className="order-last flex w-full gap-1 rounded-lg border border-line-subtle bg-canvas p-1 md:order-none md:w-auto">
            {VIEWS.map(({ value, label, short, icon: Icon }) => (
              <Tabs.Trigger
                key={value}
                value={value}
                className={cx(
                  'inline-flex min-h-touch flex-1 items-center justify-center gap-2 whitespace-nowrap rounded-md px-4 text-body-md font-semibold text-fg-secondary transition duration-150',
                  'hover:bg-surface-interactive hover:text-fg-primary active:scale-98',
                  'data-active:bg-surface-elevated data-active:text-fg-primary data-active:shadow-ambient',
                )}
              >
                <Icon className="size-4" aria-hidden />
                <span className="sm:hidden">{short}</span>
                <span className="hidden sm:inline">{label}</span>
              </Tabs.Trigger>
            ))}
          </Tabs.List>
          <div className="ml-auto">
            <SimulationControls />
          </div>
        </div>
      </header>

      <main className="flex flex-1 flex-col">
      {VIEWS.map(({ value }) => (
        <Tabs.Content key={value} value={value} className="flex flex-1 flex-col">
          <motion.div
            id="console"
            tabIndex={-1}
            initial={switched ? { opacity: 0, x: value === 'driver' ? 16 : -16 } : false}
            animate={{ opacity: 1, x: 0 }}
            className="flex flex-1 flex-col outline-none"
          >
            {value === 'passenger' ? <PassengerConsole /> : <DriverCockpit />}
          </motion.div>
        </Tabs.Content>
      ))}
      </main>
      <footer className="border-t border-line-subtle px-4 py-4 md:px-8">
        <p className="mx-auto max-w-screen-xl text-caption-sm text-fg-secondary">
          Fares are integer poysha (100 poysha = ৳1). Data comes from an in-browser dispatch simulation; payments are simulated TeslaPay.
        </p>
      </footer>
      <Notices />
    </Tabs.Root>
  );
}
