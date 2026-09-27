'use client';

import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';

import { DispatchEngine } from '@/sim/engine';
import type { World } from '@/sim/world';

const EngineContext = createContext<DispatchEngine | null>(null);

export function EngineProvider({ children, engine: injected }: { children: ReactNode; engine?: DispatchEngine }) {
  const [engine] = useState(() => injected ?? new DispatchEngine());
  useEffect(() => {
    engine.start();
    return () => engine.stop();
  }, [engine]);
  return <EngineContext.Provider value={engine}>{children}</EngineContext.Provider>;
}

export function useEngine(): DispatchEngine {
  const engine = useContext(EngineContext);
  if (!engine) throw new Error('useEngine must be used inside <EngineProvider>');
  return engine;
}

export function useWorld(): World {
  const engine = useEngine();
  return useSyncExternalStore(engine.subscribe, engine.getSnapshot, engine.getSnapshot);
}
