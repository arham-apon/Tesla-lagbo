import { describe, expect, it } from 'vitest';

import { color, type ColorToken } from './tokens';

/** WCAG 2.1 relative luminance of an opaque #RRGGBB colour. */
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(fg: ColorToken, bg: ColorToken): number {
  const [a, b] = [luminance(color[fg]), luminance(color[bg])].sort((x, y) => y - x) as [number, number];
  return (a + 0.05) / (b + 0.05);
}

const SURFACES: ColorToken[] = ['canvas-base', 'surface-raised', 'surface-elevated', 'surface-interactive'];

/**
 * Every text colour the UI actually uses, on every surface it may sit on. text-tertiary is excluded on
 * purpose: at ~4.0:1 on the canvas (and 3.0:1 on elevated cards) it fails AA, so it is only used for
 * disabled controls and decorative seat numerals, which WCAG 1.4.3 exempts.
 */
const BODY_TEXT: ColorToken[] = ['text-primary', 'text-secondary', 'accent-mint', 'warning-amber', 'danger-coral-text'];

describe('WCAG 2.1 contrast of design tokens', () => {
  it.each(BODY_TEXT.flatMap((fg) => SURFACES.map((bg) => [fg, bg] as const)))('%s on %s ≥ 4.5:1', (fg, bg) => {
    expect(ratio(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(SURFACES)('headings (text-primary) on %s ≥ 7:1', (bg) => {
    expect(ratio('text-primary', bg)).toBeGreaterThanOrEqual(7);
  });

  it('button labels on filled accents ≥ 4.5:1', () => {
    expect(ratio('text-on-accent', 'accent-mint')).toBeGreaterThanOrEqual(7);
    expect(ratio('text-on-accent', 'warning-amber')).toBeGreaterThanOrEqual(7);
  });

  it('raw danger coral is only safe for text on the darkest surfaces', () => {
    expect(ratio('danger-coral', 'surface-raised')).toBeGreaterThanOrEqual(4.5);
    expect(ratio('danger-coral', 'surface-elevated')).toBeLessThan(4.5); // hence --danger-coral-text
  });

  it('text-tertiary is genuinely below AA (documented, not used for information)', () => {
    expect(ratio('text-tertiary', 'canvas-base')).toBeLessThan(4.5);
  });
});
