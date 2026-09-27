import type { ComponentPropsWithoutRef, ReactNode } from 'react';

import { cx } from '@/utils/cx';

type Tone = 'neutral' | 'mint' | 'amber' | 'coral';

const badgeTone: Record<Tone, string> = {
  neutral: 'border-line-subtle bg-surface-interactive text-fg-secondary',
  mint: 'border-mint bg-mint-subtle text-mint',
  amber: 'border-amber bg-amber-subtle text-amber',
  coral: 'border-coral bg-coral-subtle text-coral-text',
};

export function Badge({ tone = 'neutral', icon, children, className }: { tone?: Tone; icon?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <span className={cx('inline-flex items-center gap-1 rounded-sm border px-2 py-1 text-caption-sm', badgeTone[tone], className)}>
      {icon}
      {children}
    </span>
  );
}

/** A titled surface. <section> + heading keeps the page outline navigable. */
export function Panel({
  title,
  titleId,
  action,
  children,
  className,
  elevated,
  ...rest
}: { title?: ReactNode; titleId?: string; action?: ReactNode; elevated?: boolean } & Omit<ComponentPropsWithoutRef<'section'>, 'title'>) {
  return (
    <section
      aria-labelledby={title ? titleId : undefined}
      className={cx('rounded-xl border border-line-subtle p-4 shadow-ambient', elevated ? 'bg-surface-elevated' : 'bg-surface-raised', className)}
      {...rest}
    >
      {title && (
        <header className="mb-4 flex items-center justify-between gap-2">
          <h2 id={titleId} className="text-heading-lg text-fg-primary">
            {title}
          </h2>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

/** Layout-bounded shimmer. Give it the exact size classes of what it stands in for. */
export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden className={cx('block rounded-md bg-shimmer bg-shimmer-track motion-safe:animate-shimmer', className)} />;
}

/** Live numbers: monospace + tabular figures so changing digits never shift the layout. */
export function Telemetry({
  label,
  value,
  size = 'md',
  tone = 'primary',
  srValue,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  size?: 'md' | 'lg';
  tone?: 'primary' | 'secondary' | 'mint' | 'amber' | 'coral';
  srValue?: string;
  className?: string;
}) {
  const toneClass = {
    primary: 'text-fg-primary',
    secondary: 'text-fg-secondary',
    mint: 'text-mint',
    amber: 'text-amber',
    coral: 'text-coral-text',
  }[tone];
  return (
    <div className={cx('flex flex-col gap-1', className)}>
      <dt className="text-caption-sm text-fg-secondary">{label}</dt>
      <dd className={cx('font-mono tabular-nums', size === 'lg' ? 'text-telemetry-lg' : 'text-telemetry-md', toneClass)}>
        {srValue ? (
          <>
            <span aria-hidden>{value}</span>
            <span className="sr-only">{srValue}</span>
          </>
        ) : (
          value
        )}
      </dd>
    </div>
  );
}

export function InlineMessage({ id, tone = 'coral', children }: { id?: string; tone?: 'coral' | 'amber' | 'neutral'; children: ReactNode }) {
  const toneClass = { coral: 'text-coral-text', amber: 'text-amber', neutral: 'text-fg-secondary' }[tone];
  return (
    <p id={id} className={cx('text-caption-sm', toneClass)}>
      {children}
    </p>
  );
}

/** Fixed-width placeholder for time-dependent values before the client clock starts (SSR-safe). */
export const CLOCK_PLACEHOLDER = '--:--';
