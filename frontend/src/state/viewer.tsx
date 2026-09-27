'use client';

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

import { PASSENGERS } from '@/domain/cast';
import type { ConsoleView, DhakaZone, PassengerName, SeatIndex } from '@/types/mobility';

/** A commuter's booking inputs. Kept outside the ride so a lost seat never wipes them. */
export interface RouteDraft {
  readonly pickup: DhakaZone;
  readonly dropoff: DhakaZone;
  readonly seat: SeatIndex | null;
}

interface ViewerState {
  readonly view: ConsoleView;
  readonly setView: (v: ConsoleView) => void;
  readonly persona: PassengerName;
  readonly setPersona: (p: PassengerName) => void;
  readonly drafts: Readonly<Record<PassengerName, RouteDraft>>;
  readonly updateDraft: (p: PassengerName, patch: Partial<RouteDraft>) => void;
  readonly raceArmed: boolean;
  readonly setRaceArmed: (v: boolean) => void;
}

const ViewerContext = createContext<ViewerState | null>(null);

const initialDrafts = (): Record<PassengerName, RouteDraft> => ({
  Nusrat: { pickup: PASSENGERS.Nusrat.pickupZone, dropoff: PASSENGERS.Nusrat.dropoffZone, seat: null },
  Rafiq: { pickup: PASSENGERS.Rafiq.pickupZone, dropoff: PASSENGERS.Rafiq.dropoffZone, seat: null },
  Shirin: { pickup: PASSENGERS.Shirin.pickupZone, dropoff: PASSENGERS.Shirin.dropoffZone, seat: null },
});

export function ViewerProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState<ConsoleView>('passenger');
  const [persona, setPersona] = useState<PassengerName>('Shirin');
  const [drafts, setDrafts] = useState(initialDrafts);
  const [raceArmed, setRaceArmed] = useState(false);

  const updateDraft = useCallback(
    (p: PassengerName, patch: Partial<RouteDraft>) => setDrafts((d) => ({ ...d, [p]: { ...d[p], ...patch } })),
    [],
  );

  const value = useMemo(
    () => ({ view, setView, persona, setPersona, drafts, updateDraft, raceArmed, setRaceArmed }),
    [view, persona, drafts, updateDraft, raceArmed],
  );
  return <ViewerContext.Provider value={value}>{children}</ViewerContext.Provider>;
}

export function useViewer(): ViewerState {
  const v = useContext(ViewerContext);
  if (!v) throw new Error('useViewer must be used inside <ViewerProvider>');
  return v;
}
