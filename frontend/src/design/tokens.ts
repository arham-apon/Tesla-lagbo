/**
 * Industrial Cyber-Bespoke design tokens — the single source of truth.
 *
 * tailwind.config.ts turns `color` into CSS custom properties on :root (so `--canvas-base` etc. exist
 * exactly as the guideline names them) and points every Tailwind colour utility at those variables.
 * The contrast test (src/design/contrast.test.ts) reads the same values, so a token edit that breaks
 * WCAG 2.1 AA fails CI instead of shipping.
 *
 * This is the only file in src/ allowed to hold raw colour literals (enforced by eslint `tokens/no-raw-color`).
 */

export const color = {
  // Guideline §2 — verbatim values.
  'canvas-base': '#0B0F17',
  'surface-raised': '#111827',
  'surface-elevated': '#1E293B',
  'surface-interactive': '#253349',
  'border-subtle': 'rgba(255, 255, 255, 0.08)',
  'border-strong': 'rgba(255, 255, 255, 0.20)',
  'text-primary': '#F9FAFB',
  'text-secondary': '#94A3B8',
  'text-tertiary': '#64748B',
  'accent-mint': '#10B981',
  'accent-mint-subtle': 'rgba(16, 185, 129, 0.12)',
  'warning-amber': '#F59E0B',
  'danger-coral': '#EF4444',

  // Derived tokens (not in the guideline table, added for WCAG reasons — see README "Token additions").
  // Coral text on --surface-elevated is only 3.8:1, so coral *text* uses this lighter step (5.2:1).
  'danger-coral-text': '#F87171',
  // Tinted backgrounds for the Error state ("subtle coral tinted background") and amber notices,
  // built the same way as --accent-mint-subtle (12 % of the accent).
  'danger-coral-subtle': 'rgba(239, 68, 68, 0.12)',
  'warning-amber-subtle': 'rgba(245, 158, 11, 0.12)',
  // Text/icon colour that sits ON a filled mint or amber button (7.6:1 on mint).
  'text-on-accent': '#0B0F17',
} as const;

export type ColorToken = keyof typeof color;

/** 8-point spatial ladder: spacing(n) = n × 4px, n ∈ {1, 2, 3, 4, 6, 8, 12, 16}. Nothing else exists. */
export const spacingSteps = [1, 2, 3, 4, 6, 8, 12, 16] as const;

/** Semantic sizes that are not on the ladder but are mandated by the guideline. */
export const touch = {
  target: '48px', // minimum hit area on every mobile control
  cockpit: '56px', // driver cockpit action buttons
} as const;

/** Layout offsets that are not spacing steps. */
export const layout = {
  headerOffset: '96px', // sticky panels sit below the sticky app header
} as const;

export const shadow = {
  ambient: '0 4px 20px -2px rgba(0, 0, 0, 0.5)',
  lifted: '0 8px 28px -4px rgba(0, 0, 0, 0.6)',
} as const;

/** Major Third (×1.25) scale. [size, lineHeight, weight] */
export const typeScale = {
  'display-2xl': ['28px', '36px', '800'],
  'title-xl': ['22px', '28px', '700'],
  'heading-lg': ['18px', '24px', '600'],
  'body-md': ['15px', '22px', '400'],
  'caption-sm': ['13px', '18px', '500'],
  'telemetry-lg': ['24px', '28px', '700'],
  'telemetry-md': ['14px', '20px', '500'],
} as const;

export const breakpoints = {
  xs: '360px',
  sm: '640px',
  md: '768px',
  lg: '1024px',
  xl: '1280px',
} as const;

/** Motion physics shared by every spring in the app. */
export const spring = { type: 'spring', stiffness: 400, damping: 30 } as const;

export const durations = {
  hover: 150, // ms — hover/press transitions
  shake: 300, // ms — contention shake + coral pulse
} as const;
