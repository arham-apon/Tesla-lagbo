/**
 * Money is integer poysha end to end (1 Taka = 100 poysha). Formatting is pure integer arithmetic:
 * no division into floats, so 5250 can never render as 52.499999.
 */

const group = (n: number): string => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');

function assertPoysha(poysha: number): void {
  if (!Number.isSafeInteger(poysha)) throw new TypeError(`poysha must be an integer, got ${poysha}`);
}

/** 5250 → "52.50", 750000 → "7,500.00", -2250 → "-22.50" */
export function formatTaka(poysha: number): string {
  assertPoysha(poysha);
  const sign = poysha < 0 ? '-' : '';
  const abs = Math.abs(poysha);
  return `${sign}${group(Math.trunc(abs / 100))}.${String(abs % 100).padStart(2, '0')}`;
}

/** 5250 → "৳52.50"; negatives use a true minus sign so screen readers say "minus". */
export function formatTakaSymbol(poysha: number): string {
  const s = formatTaka(Math.abs(poysha));
  return poysha < 0 ? `−৳${s}` : `৳${s}`;
}

/** 5250 → "5,250 poysha" */
export function formatPoysha(poysha: number): string {
  assertPoysha(poysha);
  return `${poysha < 0 ? '-' : ''}${group(Math.abs(poysha))} poysha`;
}

/** Spoken form for aria-labels: 5250 → "52 taka 50 poysha" */
export function speakTaka(poysha: number): string {
  assertPoysha(poysha);
  const abs = Math.abs(poysha);
  const taka = Math.trunc(abs / 100);
  const rest = abs % 100;
  return `${poysha < 0 ? 'minus ' : ''}${taka} taka${rest ? ` ${rest} poysha` : ''}`;
}
