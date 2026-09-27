'use client';

import { useCallback, useSyncExternalStore } from 'react';

/** One shared ticking clock per interval, exposed as an external store. */
interface Clock {
  now: number;
  readonly listeners: Set<() => void>;
  timer: ReturnType<typeof setInterval> | null;
}

const clocks = new Map<number, Clock>();

function clockFor(intervalMs: number): Clock {
  let c = clocks.get(intervalMs);
  if (!c) {
    c = { now: Date.now(), listeners: new Set(), timer: null };
    clocks.set(intervalMs, c);
  }
  return c;
}

/**
 * Wall clock for countdowns. Returns null during SSR and hydration, so server and client markup match;
 * time-dependent text renders a fixed-width placeholder until the clock is live.
 */
export function useNow(intervalMs = 1000): number | null {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const c = clockFor(intervalMs);
      c.listeners.add(onChange);
      if (!c.timer) {
        c.now = Date.now();
        c.timer = setInterval(() => {
          c.now = Date.now();
          for (const l of c.listeners) l();
        }, intervalMs);
      }
      return () => {
        c.listeners.delete(onChange);
        if (!c.listeners.size && c.timer) {
          clearInterval(c.timer);
          c.timer = null;
        }
      };
    },
    [intervalMs],
  );
  const getSnapshot = useCallback(() => clockFor(intervalMs).now, [intervalMs]);
  return useSyncExternalStore(subscribe, getSnapshot, () => null);
}
