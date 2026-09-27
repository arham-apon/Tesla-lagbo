'use client';

import { motion, useAnimationControls, useReducedMotion } from 'framer-motion';
import { LoaderCircle } from 'lucide-react';
import { forwardRef, useEffect, type ComponentPropsWithoutRef, type MouseEvent, type ReactNode } from 'react';

import { cx } from '@/utils/cx';

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
type Size = 'md' | 'cockpit';

type NativeProps = Omit<
  ComponentPropsWithoutRef<'button'>,
  'disabled' | 'style' | 'onAnimationStart' | 'onAnimationEnd' | 'onAnimationIteration' | 'onDrag' | 'onDragStart' | 'onDragEnd'
>;

export interface ButtonProps extends NativeProps {
  readonly variant?: Variant;
  readonly size?: Size;
  readonly fullWidth?: boolean;
  /** Loading: aria-busy, label swapped in place (no layout shift), clicks ignored. */
  readonly loading?: boolean;
  readonly loadingLabel?: string;
  /** Disabled: aria-disabled (stays focusable so its reason can be read), 40 % opacity, monochrome. */
  readonly disabled?: boolean;
  /** Error: coral outline + tint, linked to its message; the shake replays whenever errorKey changes. */
  readonly errorId?: string;
  readonly errorKey?: number;
  readonly icon?: ReactNode;
}

const palette: Record<Variant, string> = {
  primary: 'bg-mint text-fg-on-accent border-mint',
  secondary: 'bg-surface-elevated text-fg-primary border-line-strong hover:bg-surface-interactive',
  danger: 'bg-coral-subtle text-coral-text border-coral',
  ghost: 'bg-transparent text-fg-secondary border-transparent hover:bg-surface-interactive hover:text-fg-primary',
};

const sizes: Record<Size, string> = {
  md: 'min-h-touch px-4 py-3 text-body-md',
  cockpit: 'min-h-touch-cockpit px-6 py-3 text-heading-lg',
};

/** The seven-state button: default, hover, active, focus-visible, disabled, loading, error. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', fullWidth, loading, loadingLabel, disabled, errorId, errorKey, icon, className, children, onClick, ...rest },
  ref,
) {
  const controls = useAnimationControls();
  const reduced = useReducedMotion();
  const inert = !!(disabled || loading);
  const hasError = !!errorId && !inert;

  useEffect(() => {
    if (!errorKey) return;
    void controls.start(
      reduced
        ? { opacity: [1, 0.55, 1], transition: { duration: 0.3 } }
        : { x: [0, -6, 6, -6, 6, 0], transition: { duration: 0.3, ease: 'easeInOut' } },
    );
  }, [errorKey, controls, reduced]);

  const handleClick = (e: MouseEvent<HTMLButtonElement>) => {
    if (inert) {
      e.preventDefault();
      return;
    }
    onClick?.(e);
  };

  return (
    <motion.button
      ref={ref}
      type="button"
      animate={controls}
      {...rest}
      onClick={handleClick}
      aria-disabled={disabled || undefined}
      aria-busy={loading || undefined}
      aria-invalid={hasError || undefined}
      aria-describedby={cx(rest['aria-describedby'], hasError && errorId) || undefined}
      className={cx(
        'relative inline-grid min-w-touch select-none place-items-center rounded-lg border font-semibold',
        'transition duration-150 hover:shadow-ambient hover:brightness-108 active:scale-98',
        'aria-busy:cursor-progress aria-disabled:cursor-not-allowed aria-disabled:opacity-40',
        'aria-disabled:scale-100 aria-disabled:shadow-none aria-disabled:brightness-100',
        sizes[size],
        disabled
          ? 'border-line-subtle bg-surface-elevated text-fg-tertiary'
          : hasError
            ? 'border-coral bg-coral-subtle text-coral-text'
            : palette[variant],
        fullWidth && 'w-full',
        className,
      )}
    >
      {/* Both labels share one grid cell, so a button that can load is always as wide as its longer label. */}
      <span className={cx('col-start-1 row-start-1 inline-flex items-center gap-2', loading && 'invisible')}>
        {icon}
        {children}
      </span>
      {loading !== undefined && (
        <span className={cx('col-start-1 row-start-1 inline-flex items-center gap-2', !loading && 'invisible')} aria-hidden={!loading}>
          <LoaderCircle className="size-4 motion-safe:animate-spin" aria-hidden />
          {loadingLabel ?? 'Working...'}
        </span>
      )}
    </motion.button>
  );
});
