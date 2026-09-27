import type { Config } from 'tailwindcss';
import plugin from 'tailwindcss/plugin';

import { breakpoints, color, layout, shadow, spacingSteps, touch, typeScale } from './src/design/tokens';

const v = (name: keyof typeof color) => `var(--${name})`;

/** spacing: n → n×4px for the ladder only. p-5, gap-7, mt-10 … simply do not exist. */
const spacing: Record<string, string> = { 0: '0px', px: '1px' };
for (const n of spacingSteps) spacing[n] = `${n * 4}px`;
spacing.touch = touch.target;
spacing['touch-cockpit'] = touch.cockpit;
spacing.header = layout.headerOffset;

const fontSize = Object.fromEntries(
  Object.entries(typeScale).map(([k, [size, lineHeight, fontWeight]]) => [k, [size, { lineHeight, fontWeight }]]),
) as Record<string, [string, { lineHeight: string; fontWeight: string }]>;

export default {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    // Everything below *replaces* Tailwind's defaults: only tokens can be used.
    screens: breakpoints,
    spacing,
    fontSize,
    colors: {
      transparent: 'transparent',
      current: 'currentColor',
      canvas: v('canvas-base'),
      'surface-raised': v('surface-raised'),
      'surface-elevated': v('surface-elevated'),
      'surface-interactive': v('surface-interactive'),
      'line-subtle': v('border-subtle'),
      'line-strong': v('border-strong'),
      'fg-primary': v('text-primary'),
      'fg-secondary': v('text-secondary'),
      'fg-tertiary': v('text-tertiary'),
      'fg-on-accent': v('text-on-accent'),
      mint: v('accent-mint'),
      'mint-subtle': v('accent-mint-subtle'),
      amber: v('warning-amber'),
      'amber-subtle': v('warning-amber-subtle'),
      coral: v('danger-coral'),
      'coral-text': v('danger-coral-text'),
      'coral-subtle': v('danger-coral-subtle'),
    },
    fontFamily: {
      sans: ['var(--font-inter)', 'Inter', '-apple-system', 'sans-serif'],
      mono: ['var(--font-mono)', 'JetBrains Mono', 'ui-monospace', 'monospace'],
    },
    fontWeight: { normal: '400', medium: '500', semibold: '600', bold: '700', extrabold: '800' },
    borderRadius: { none: '0px', sm: '4px', md: '8px', lg: '12px', xl: '16px', full: '9999px' },
    borderWidth: { DEFAULT: '1px', 0: '0px', 2: '2px' },
    outlineWidth: { 0: '0px', 2: '2px' },
    outlineOffset: { 0: '0px', 2: '2px' },
    boxShadow: { none: 'none', ambient: shadow.ambient, lifted: shadow.lifted },
    brightness: { 100: '1', 108: '1.08' },
    scale: { 98: '0.98', 100: '1' },
    transitionDuration: { 150: '150ms', 300: '300ms' },
    extend: {
      aria: { 'current-step': 'current="step"' },
      data: {
        open: 'state="open"',
        closed: 'state="closed"',
        checked: 'state="checked"',
        unchecked: 'state="unchecked"',
        active: 'state="active"',
        highlighted: 'highlighted',
        placeholder: 'placeholder',
      },
      keyframes: {
        shimmer: { '0%': { backgroundPosition: '200% 0' }, '100%': { backgroundPosition: '-200% 0' } },
        'seat-open': {
          '0%, 100%': { borderColor: v('accent-mint') },
          '50%': { borderColor: v('border-strong') },
        },
        'contention-pulse': {
          '0%': { borderColor: v('danger-coral'), backgroundColor: v('danger-coral-subtle') },
          '100%': { borderColor: v('border-subtle'), backgroundColor: v('surface-elevated') },
        },
        radar: { '0%': { transform: 'scale(0.4)', opacity: '0.9' }, '100%': { transform: 'scale(1)', opacity: '0' } },
        'accordion-down': { from: { height: '0' }, to: { height: 'var(--radix-accordion-content-height)' } },
        'accordion-up': { from: { height: 'var(--radix-accordion-content-height)' }, to: { height: '0' } },
      },
      animation: {
        shimmer: 'shimmer 1.6s linear infinite',
        'seat-open': 'seat-open 2s ease-in-out infinite',
        'contention-pulse': 'contention-pulse 300ms ease-out 1',
        radar: 'radar 2s ease-out infinite',
        'accordion-down': 'accordion-down 200ms ease-out',
        'accordion-up': 'accordion-up 200ms ease-out',
      },
      backgroundImage: {
        shimmer: `linear-gradient(90deg, ${v('surface-elevated')} 25%, ${v('surface-interactive')} 50%, ${v('surface-elevated')} 75%)`,
      },
      backgroundSize: { 'shimmer-track': '200% 100%' },
    },
  },
  plugins: [
    plugin(({ addBase }) => {
      addBase({
        ':root': Object.fromEntries(Object.entries(color).map(([k, val]) => [`--${k}`, val])),
      });
    }),
  ],
} satisfies Config;
