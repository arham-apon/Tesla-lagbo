'use client';

import { AnimatePresence, motion } from 'framer-motion';
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';

import { useAnnounce } from '@/components/a11y/Announcer';
import { useWorld } from '@/state/EngineProvider';
import { useViewer } from '@/state/viewer';
import type { Notice, NoticeTone } from '@/sim/world';
import { cx } from '@/utils/cx';

const TOAST_MS = 5000;

const toneStyle: Record<NoticeTone, { box: string; icon: typeof Info }> = {
  info: { box: 'border-line-strong', icon: Info },
  success: { box: 'border-mint', icon: CircleCheck },
  warning: { box: 'border-amber', icon: TriangleAlert },
  danger: { box: 'border-coral', icon: CircleAlert },
};

const iconTone: Record<NoticeTone, string> = {
  info: 'text-fg-secondary',
  success: 'text-mint',
  warning: 'text-amber',
  danger: 'text-coral-text',
};

/**
 * Bridges server notices to the person currently looking at the screen: polite ones are spoken through
 * the live region and shown as a toast. Urgent ones (seat lockouts) are rendered in place by the
 * contention banner (role="alert"), so they are not duplicated here.
 */
export function Notices() {
  const world = useWorld();
  const { view, persona } = useViewer();
  const announce = useAnnounce();
  const [toasts, setToasts] = useState<Notice[]>([]);
  const lastSeen = useRef<number | null>(null);

  const audience = view === 'driver' ? 'driver' : persona;

  useEffect(() => {
    const newest = world.notices.at(-1)?.id ?? 0;
    if (lastSeen.current === null) {
      lastSeen.current = newest; // don't replay history on first mount
      return;
    }
    const fresh = world.notices.filter((n) => n.id > lastSeen.current!);
    lastSeen.current = Math.max(lastSeen.current, newest);
    const mine = fresh.filter((n) => n.audience === audience && !n.urgent);
    if (!mine.length) return;
    for (const n of mine) announce(n.text, 'polite');
    setToasts((t) => [...t, ...mine].slice(-3));
  }, [world.notices, audience, announce]);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((n) => n.id !== id)), []);

  return (
    <section aria-label="Recent updates" className="pointer-events-none fixed inset-x-4 top-4 z-50 md:left-auto md:right-8 md:w-full md:max-w-sm">
      <ol className="flex flex-col gap-2">
        <AnimatePresence initial={false}>
          {/* Switching persona or console hides toasts meant for someone else. */}
          {toasts.filter((n) => n.audience === audience).map((n) => (
            <Toast key={n.id} notice={n} onDismiss={dismiss} />
          ))}
        </AnimatePresence>
      </ol>
    </section>
  );
}

function Toast({ notice, onDismiss }: { notice: Notice; onDismiss: (id: number) => void }) {
  useEffect(() => {
    const t = setTimeout(() => onDismiss(notice.id), TOAST_MS);
    return () => clearTimeout(t);
  }, [notice.id, onDismiss]);
  const { box, icon: Icon } = toneStyle[notice.tone];
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className={cx('pointer-events-auto flex items-start gap-3 rounded-lg border-2 bg-surface-elevated py-2 pl-4 pr-1 shadow-lifted', box)}
    >
      <Icon className={cx('mt-3 size-4 shrink-0', iconTone[notice.tone])} aria-hidden />
      <p className="flex-1 py-2 text-body-md text-fg-primary">{notice.text}</p>
      <button
        type="button"
        onClick={() => onDismiss(notice.id)}
        aria-label="Dismiss update"
        className="grid size-12 shrink-0 place-items-center rounded-md text-fg-secondary transition duration-150 hover:bg-surface-interactive hover:text-fg-primary active:scale-98"
      >
        <X className="size-4" aria-hidden />
      </button>
    </motion.li>
  );
}
