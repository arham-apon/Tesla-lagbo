'use client';

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react';

type Politeness = 'polite' | 'assertive';
type Announce = (message: string, politeness?: Politeness) => void;

const AnnouncerContext = createContext<Announce>(() => {});

/**
 * Two always-mounted live regions. polite: seat reservations, arrivals, fare updates.
 * assertive: seat lockouts only. The regions exist before any message so screen readers register them.
 */
export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [polite, setPolite] = useState('');
  const [assertive, setAssertive] = useState('');
  const flip = useRef(false);

  const announce = useCallback<Announce>((message, politeness = 'polite') => {
    // A trailing zero-width space on every other call makes a repeated identical message count as a change.
    flip.current = !flip.current;
    const text = flip.current ? message : `${message}​`;
    (politeness === 'assertive' ? setAssertive : setPolite)(text);
  }, []);

  return (
    <AnnouncerContext.Provider value={announce}>
      {children}
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="live-polite">
        {polite}
      </div>
      <div className="sr-only" aria-live="assertive" aria-atomic="true" data-testid="live-assertive">
        {assertive}
      </div>
    </AnnouncerContext.Provider>
  );
}

export const useAnnounce = (): Announce => useContext(AnnouncerContext);
