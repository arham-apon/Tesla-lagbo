'use client';

import * as RadixSelect from '@radix-ui/react-select';
import * as RadixSwitch from '@radix-ui/react-switch';
import { animate, motion, useMotionValue, useReducedMotion, useTransform } from 'framer-motion';
import { Check, ChevronDown, ChevronsRight, LoaderCircle } from 'lucide-react';
import { useId, useRef, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react';

import { spring } from '@/design/tokens';
import { cx } from '@/utils/cx';

import { InlineMessage } from './primitives';

// ---- Switch ------------------------------------------------------------------------------------------------------

export interface SwitchFieldProps {
  readonly label: ReactNode;
  readonly description?: ReactNode;
  readonly checked: boolean;
  readonly onCheckedChange: (checked: boolean) => void;
  readonly disabled?: boolean;
  readonly disabledReason?: string;
  readonly loading?: boolean;
  readonly error?: string;
}

/** Labelled switch with all seven states. The whole row is the 48 px hit target. */
export function SwitchField({ label, description, checked, onCheckedChange, disabled, disabledReason, loading, error }: SwitchFieldProps) {
  const id = useId();
  const inert = disabled || loading;
  const described = [description && `${id}-desc`, disabledReason && disabled && `${id}-why`, error && `${id}-err`].filter(Boolean).join(' ');
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor={id} className="text-body-md font-semibold text-fg-primary">
            {label}
          </label>
          {description && (
            <p id={`${id}-desc`} className="text-caption-sm text-fg-secondary">
              {description}
            </p>
          )}
        </div>
        <RadixSwitch.Root
          id={id}
          checked={checked}
          onCheckedChange={(v) => !inert && onCheckedChange(v)}
          aria-disabled={disabled || undefined}
          aria-busy={loading || undefined}
          aria-invalid={!!error || undefined}
          aria-describedby={described || undefined}
          className={cx(
            'group inline-flex min-h-touch min-w-touch shrink-0 items-center justify-center rounded-full transition duration-150',
            'hover:brightness-108 active:scale-98 aria-disabled:cursor-not-allowed aria-disabled:opacity-40 aria-busy:cursor-progress',
          )}
        >
          <span
            className={cx(
              'flex h-8 w-16 items-center rounded-full border-2 p-1 transition duration-150',
              'justify-start border-line-strong bg-surface-interactive group-data-checked:justify-end group-data-checked:border-mint group-data-checked:bg-mint-subtle',
              error && 'border-coral',
            )}
          >
            <RadixSwitch.Thumb asChild>
              <motion.span layout transition={spring} className="grid size-6 place-items-center rounded-full bg-fg-primary group-data-checked:bg-mint">
                {loading && <LoaderCircle className="size-4 text-canvas motion-safe:animate-spin" aria-hidden />}
              </motion.span>
            </RadixSwitch.Thumb>
          </span>
        </RadixSwitch.Root>
      </div>
      {disabled && disabledReason && (
        <InlineMessage id={`${id}-why`} tone="neutral">
          {disabledReason}
        </InlineMessage>
      )}
      {error && <InlineMessage id={`${id}-err`}>{error}</InlineMessage>}
    </div>
  );
}

// ---- Select ------------------------------------------------------------------------------------------------------

export interface SelectOption {
  readonly value: string;
  readonly label: string;
  readonly hint?: string;
  readonly disabled?: boolean;
}

export interface SelectFieldProps {
  readonly label: string;
  readonly value: string;
  readonly options: readonly SelectOption[];
  readonly onValueChange: (value: string) => void;
  readonly disabled?: boolean;
  readonly icon?: ReactNode;
  readonly error?: string;
}

export function SelectField({ label, value, options, onValueChange, disabled, icon, error }: SelectFieldProps) {
  const id = useId();
  return (
    <div className="flex flex-col gap-2">
      <span id={`${id}-label`} className="text-caption-sm text-fg-secondary">
        {label}
      </span>
      <RadixSelect.Root value={value} onValueChange={onValueChange} disabled={disabled}>
        <RadixSelect.Trigger
          aria-labelledby={`${id}-label`}
          aria-invalid={!!error || undefined}
          aria-describedby={error ? `${id}-err` : undefined}
          className={cx(
            'flex min-h-touch w-full items-center justify-between gap-2 rounded-lg border-2 bg-surface-elevated px-3 py-2 text-left text-body-md text-fg-primary',
            'transition duration-150 hover:bg-surface-interactive focus-visible:border-mint',
            'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-surface-elevated',
            error ? 'border-coral' : 'border-line-subtle',
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            {icon}
            <RadixSelect.Value />
          </span>
          <RadixSelect.Icon>
            <ChevronDown className="size-4 text-fg-secondary" aria-hidden />
          </RadixSelect.Icon>
        </RadixSelect.Trigger>
        <RadixSelect.Portal>
          <RadixSelect.Content className="z-50 overflow-hidden rounded-lg border border-line-strong bg-surface-elevated shadow-lifted">
            <RadixSelect.Viewport className="p-1">
              {options.map((o) => (
                <RadixSelect.Item
                  key={o.value}
                  value={o.value}
                  disabled={o.disabled}
                  className={cx(
                    'relative flex min-h-touch cursor-pointer select-none items-center justify-between gap-4 rounded-md py-2 pl-8 pr-3 text-body-md text-fg-primary outline-none',
                    'data-highlighted:bg-surface-interactive data-disabled:cursor-not-allowed data-disabled:opacity-40',
                  )}
                >
                  <RadixSelect.ItemIndicator className="absolute left-2 inline-flex">
                    <Check className="size-4 text-mint" aria-hidden />
                  </RadixSelect.ItemIndicator>
                  <RadixSelect.ItemText>{o.label}</RadixSelect.ItemText>
                  {o.hint && <span className="text-caption-sm text-mint">{o.hint}</span>}
                </RadixSelect.Item>
              ))}
            </RadixSelect.Viewport>
          </RadixSelect.Content>
        </RadixSelect.Portal>
      </RadixSelect.Root>
      {error && <InlineMessage id={`${id}-err`}>{error}</InlineMessage>}
    </div>
  );
}

// ---- Slide to confirm --------------------------------------------------------------------------------------------

export interface SlideToConfirmProps {
  readonly label: string;
  readonly onConfirm: () => void;
  readonly loading?: boolean;
  readonly loadingLabel?: string;
  readonly errorId?: string;
}

/**
 * A slide control for irreversible in-traffic actions: a stray tap can't board a passenger.
 * Keyboard and assistive-tech users activate the handle directly (Enter/Space, or a synthetic click with
 * detail 0), which is just as deliberate.
 */
export function SlideToConfirm({ label, onConfirm, loading, loadingLabel, errorId }: SlideToConfirmProps) {
  const track = useRef<HTMLDivElement>(null);
  const x = useMotionValue(0);
  const fill = useTransform(x, (v) => v + 52);
  const reduced = useReducedMotion();

  const reset = () => (reduced ? x.set(0) : void animate(x, 0, spring));

  const onDragEnd = () => {
    const width = track.current?.clientWidth ?? 0;
    if (width && x.get() >= (width - 56) * 0.85) onConfirm();
    reset();
  };
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (!loading) onConfirm();
    }
  };
  const onClick = (e: MouseEvent) => {
    if (e.detail === 0 && !loading) onConfirm(); // screen-reader / switch-access activation
  };

  return (
    <div
      ref={track}
      className="relative flex min-h-touch-cockpit select-none items-center overflow-hidden rounded-xl border-2 border-mint bg-mint-subtle"
    >
      <motion.span aria-hidden className="absolute inset-y-0 left-0 bg-mint-subtle" style={{ width: fill }} />
      <span aria-hidden className="w-full px-16 text-center text-body-md font-semibold text-mint">
        {loading ? (loadingLabel ?? 'Confirming...') : label}
      </span>
      <motion.button
        type="button"
        drag={loading ? false : 'x'}
        dragConstraints={track}
        dragElastic={0}
        dragMomentum={false}
        onDragEnd={onDragEnd}
        onKeyDown={onKeyDown}
        onClick={onClick}
        style={{ x }}
        aria-label={`${label}. Slide right, or press Enter.`}
        aria-busy={loading || undefined}
        aria-describedby={errorId}
        className="absolute left-1 top-1 grid size-12 cursor-grab touch-none place-items-center rounded-lg bg-mint text-fg-on-accent shadow-ambient active:cursor-grabbing aria-busy:cursor-progress"
      >
        {loading ? <LoaderCircle className="size-6 motion-safe:animate-spin" aria-hidden /> : <ChevronsRight className="size-6" aria-hidden />}
      </motion.button>
    </div>
  );
}

// ---- Gauges ------------------------------------------------------------------------------------------------------

/** Circular countdown for the 15-second ride offer. SVG attributes only, no inline styles. */
export function CountdownRing({ remainingMs, totalMs, label }: { remainingMs: number; totalMs: number; label: string }) {
  const r = 22;
  const c = 2 * Math.PI * r;
  const frac = Math.max(0, Math.min(1, remainingMs / totalMs));
  const secs = Math.ceil(Math.max(0, remainingMs) / 1000);
  const urgent = secs <= 5;
  return (
    <div className="relative grid size-16 shrink-0 place-items-center" role="timer" aria-label={`${label}: ${secs} seconds left`}>
      <svg viewBox="0 0 56 56" className="absolute inset-0 size-16 -rotate-90" aria-hidden>
        <circle cx="28" cy="28" r={r} fill="none" strokeWidth="4" className="stroke-surface-interactive" />
        <circle
          cx="28"
          cy="28"
          r={r}
          fill="none"
          strokeWidth="4"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - frac)}
          className={cx('transition duration-300', urgent ? 'stroke-amber' : 'stroke-mint')}
        />
      </svg>
      <span aria-hidden className={cx('font-mono text-telemetry-md tabular-nums', urgent ? 'text-amber' : 'text-fg-primary')}>
        {String(secs).padStart(2, '0')}
      </span>
    </div>
  );
}

export function SeatPips({ occupied, capacity }: { occupied: number; capacity: number }) {
  return (
    <span className="inline-flex gap-1" aria-hidden>
      {Array.from({ length: capacity }, (_, i) => (
        <span key={i} className={cx('h-3 w-4 rounded-sm border', i < occupied ? 'border-mint bg-mint' : 'border-line-strong bg-transparent')} />
      ))}
    </span>
  );
}

export function BatteryGauge({ percent }: { percent: number }) {
  const width = Math.round((Math.max(0, Math.min(100, percent)) / 100) * 20);
  const tone = percent <= 20 ? 'fill-coral' : percent <= 40 ? 'fill-amber' : 'fill-mint';
  return (
    <svg viewBox="0 0 28 14" className="h-4 w-8" aria-hidden>
      <rect x="0.5" y="0.5" width="24" height="13" rx="3" fill="none" className="stroke-line-strong" />
      <rect x="25" y="4.5" width="2.5" height="5" rx="1" className="fill-line-strong" />
      <rect x="2.5" y="2.5" width={width} height="9" rx="1.5" className={tone} />
    </svg>
  );
}
