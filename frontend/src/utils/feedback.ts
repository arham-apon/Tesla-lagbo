/** Physical feedback, all best-effort: unsupported browsers and blocked audio simply do nothing. */

export function vibrate(pattern: number | number[]): void {
  if (typeof navigator !== 'undefined' && 'vibrate' in navigator) navigator.vibrate(pattern);
}

let ctx: AudioContext | null = null;

/** A short two-tone chime for "matched". */
export function playMatchChime(): void {
  if (typeof window === 'undefined' || !('AudioContext' in window)) return;
  try {
    ctx ??= new AudioContext();
    const start = ctx.currentTime;
    [880, 1318.5].forEach((freq, i) => {
      const osc = ctx!.createOscillator();
      const gain = ctx!.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, start + i * 0.14);
      gain.gain.exponentialRampToValueAtTime(0.12, start + i * 0.14 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + i * 0.14 + 0.25);
      osc.connect(gain).connect(ctx!.destination);
      osc.start(start + i * 0.14);
      osc.stop(start + i * 0.14 + 0.3);
    });
  } catch {
    /* audio is a nicety */
  }
}
