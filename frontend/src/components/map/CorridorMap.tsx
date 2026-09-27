'use client';

import { motion } from 'framer-motion';
import { useId, type ReactNode } from 'react';

import type { DhakaZone } from '@/types/mobility';
import { ZONES, zoneInfo } from '@/domain/zones';
import { poolMembers, type World } from '@/sim/world';
import { cx } from '@/utils/cx';

const W = 300;
const H = 440;
const PAD = 36;
const LAT = { min: 23.74, max: 23.885 };
const LNG = { min: 90.36, max: 90.465 };

function project(zone: DhakaZone): { x: number; y: number } {
  const z = zoneInfo(zone);
  return {
    x: PAD + ((z.lng - LNG.min) / (LNG.max - LNG.min)) * (W - 2 * PAD),
    y: PAD + ((LAT.max - z.lat) / (LAT.max - LAT.min)) * (H - 2 * PAD),
  };
}

// Neighbouring zones are ~20 px apart at this scale, so their labels go on opposite sides;
// zones near the right edge label leftwards so nothing is clipped.
const LABEL_LEFT = new Set<DhakaZone>(['Banani', 'Mohakhali', 'Farmgate', 'Bashundhara']);

/** The pool's path: pickup, then each drop-off in planned order. */
export function poolPath(w: World): DhakaZone[] {
  if (!w.pool) return [];
  const live = new Set(poolMembers(w).map((r) => r.id));
  const drops = w.pool.dropOrder.filter((id) => live.has(id)).map((id) => w.rides[id]!.dropoffZone);
  return [w.pool.pickupZone, ...drops];
}

export function CorridorMap({ world, overlay, className }: { world: World; overlay?: ReactNode; className?: string }) {
  const titleId = useId();
  const descId = useId();
  const path = poolPath(world);
  const onPath = new Set(path);
  const bullet = project(world.vehicle.currentZone);
  const points = path.map((z) => project(z));
  const description = path.length
    ? `Bullet is at ${world.vehicle.currentZone}. Pool route: ${path.join(' → ')}.`
    : `Bullet is at ${world.vehicle.currentZone} with no active route.`;

  return (
    <figure className={cx('relative overflow-hidden rounded-xl border border-line-subtle bg-surface-raised shadow-ambient', className)}>
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-labelledby={`${titleId} ${descId}`}>
        <title id={titleId}>Corridor map</title>
        <desc id={descId}>{description}</desc>

        {/* faint grid */}
        {Array.from({ length: 9 }, (_, i) => (
          <line key={`h${i}`} x1="0" x2={W} y1={(i * H) / 8} y2={(i * H) / 8} className="stroke-line-subtle" strokeWidth="1" />
        ))}
        {Array.from({ length: 7 }, (_, i) => (
          <line key={`v${i}`} y1="0" y2={H} x1={(i * W) / 6} x2={(i * W) / 6} className="stroke-line-subtle" strokeWidth="1" />
        ))}

        {points.length > 1 && (
          <motion.polyline
            key={path.join('|')}
            points={points.map((p) => `${p.x},${p.y}`).join(' ')}
            fill="none"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="stroke-mint"
            initial={{ pathLength: 0 }}
            animate={{ pathLength: 1 }}
            transition={{ duration: 0.8, ease: 'easeOut' }}
          />
        )}

        {ZONES.map((z) => {
          const p = project(z.name);
          const left = LABEL_LEFT.has(z.name);
          const active = onPath.has(z.name);
          return (
            <g key={z.name}>
              <circle cx={p.x} cy={p.y} r={active ? 6 : 4} className={active ? 'fill-mint' : 'fill-surface-interactive stroke-line-strong'} strokeWidth="1" />
              <text
                x={left ? p.x - 10 : p.x + 10}
                y={p.y + 4}
                textAnchor={left ? 'end' : 'start'}
                className={cx('text-caption-sm', active ? 'fill-fg-primary' : 'fill-fg-secondary')}
              >
                {z.name}
              </text>
            </g>
          );
        })}

        <motion.g initial={false} animate={{ x: bullet.x, y: bullet.y }}>
          <circle r="14" className="fill-mint-subtle stroke-mint" strokeWidth="2" />
          <circle r="5" className="fill-mint" />
        </motion.g>
      </svg>
      {overlay && <figcaption className="absolute inset-x-4 top-4 flex flex-wrap gap-2">{overlay}</figcaption>}
    </figure>
  );
}

export function MapChip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-md border border-line-subtle bg-surface-elevated px-3 py-2 text-caption-sm text-fg-primary shadow-ambient">
      {children}
    </span>
  );
}
