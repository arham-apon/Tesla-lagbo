'use client';

import { useCallback, useRef, useState } from 'react';

import type { Rejection, Result } from '@/sim/world';

import { describeRejection } from './messages';

export type CommandState<T> =
  | { readonly phase: 'idle' }
  | { readonly phase: 'pending' }
  | { readonly phase: 'error'; readonly error: Rejection; readonly message: string; readonly attempt: number }
  | { readonly phase: 'success'; readonly value: T };

/**
 * Async command state for one control: idle → pending → success | error. A second tap while pending is
 * ignored (no double bookings from impatient thumbs). `attempt` changes on every failure so the control can
 * replay its shake animation even when the same error repeats.
 */
export function useCommand<A extends unknown[], T>(fn: (...args: A) => Promise<Result<T>>) {
  const [state, setState] = useState<CommandState<T>>({ phase: 'idle' });
  const busy = useRef(false);
  const attempts = useRef(0);

  const run = useCallback(
    async (...args: A): Promise<Result<T> | null> => {
      if (busy.current) return null;
      busy.current = true;
      setState({ phase: 'pending' });
      try {
        const result = await fn(...args);
        if (result.ok) setState({ phase: 'success', value: result.value });
        else setState({ phase: 'error', error: result.error, message: describeRejection(result.error), attempt: ++attempts.current });
        return result;
      } finally {
        busy.current = false;
      }
    },
    [fn],
  );

  const reset = useCallback(() => setState({ phase: 'idle' }), []);
  return { state, run, reset, pending: state.phase === 'pending' } as const;
}
