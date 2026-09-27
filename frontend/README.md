# Dhaka Tesla Pool — frontend

Passenger console and driver cockpit for the Banani corridor story: Jashim drives **Bullet** (3 seats), Nusrat and Rafiq share it, and Shirin contends for the last seat. It's built to the *Frontend Guideline* (Industrial Cyber-Bespoke design system) with Next.js (App Router), TypeScript, Tailwind CSS, Radix UI primitives and Framer Motion.

```bash
npm install
npm run dev        # http://localhost:3000
npm run verify     # typecheck + lint (incl. token rules) + unit tests + production build
npm run e2e        # Playwright + axe-core on phone and desktop (uses your installed Chrome)
```

The **Simulation** button in the header resets the story ("Pool forming" or "Empty Bullet") and can turn on a flaky network (every third request fails), so you can see the error states.

## How it's put together

```
src/
  design/tokens.ts        the only source of colours, spacing, type, shadows, motion (→ Tailwind + :root vars)
  types/mobility.ts       the guideline's domain contracts (DhakaZone, RideLifecycleStatus, VehicleSeatSlot…)
  domain/                 money (integer poysha), fare model, lifecycle state machine, zones, PRD cast
  sim/world.ts            pure dispatch "server": seat locks, pool planner, offers, settlement (no I/O)
  sim/engine.ts           async shell: latency, 1 s ticker, flaky network, subscribe/getSnapshot
  components/dispatch/    CabinSeatManifest (the guideline's reference component)
  components/passenger/   route picker, trip progress, status card, fare panel, contention, action dock
  components/driver/      status bar, offer countdown, next stop, pipeline, manifest, ledger/history
  components/ui/          seven-state Button, Switch, Select, SlideToConfirm, gauges
eslint-rules/tokens.mjs   no-arbitrary-tailwind, no-inline-style, no-raw-color
e2e/                      Playwright verification-matrix checks
```

Views never keep `isLoading`/`isMatched` flags. They derive everything from one `RideLifecycleStatus`, and every transition goes through `canTransition` (`domain/lifecycle.ts`). The UI talks to the dispatch engine only through async commands, the same way it would talk to the gateway.

## Guideline compliance

| Guideline item | Where | How it's verified |
|---|---|---|
| Semantic colour tokens (`--canvas-base` … `--danger-coral`) | `design/tokens.ts` → `:root` vars via `tailwind.config.ts` | `contrast.test.ts` |
| 8-pt spacing ladder {1,2,3,4,6,8,12,16}×4 px only | `tailwind.config.ts` replaces the spacing scale (`p-5` doesn't exist) | build output |
| No arbitrary values or inline styles | `eslint-rules/tokens.mjs` | `npm run lint`, `tokens.test.mjs` |
| Major Third type scale; Inter + JetBrains Mono; tabular figures | `typeScale`, `layout.tsx`, `Telemetry` | e2e checks `font-variant-numeric` |
| Five breakpoints (xs 360 → xl 1280); map canvas on lg, 3 panes on xl | `breakpoints`, console grids | e2e: no horizontal overflow |
| Seven component states | `ui/Button.tsx`, `ui/controls.tsx`, seat cards | `Button.test.tsx` |
| Lifecycle IDLE → … → COMPLETED / CANCELLED | `domain/lifecycle.ts` | `lifecycle.test.ts` |
| Per-state affordances (radar, driver card, arrival banner and haptics, match chime, live ETA, receipt and rating, cancel reason) | `passenger/RideStatusCard.tsx`, `driver/*` | `world.test.ts`, e2e |
| Seat contention: "Securing Seat...", 6 px / 300 ms shake, seat morphs to "Nusrat", assertive banner, inputs kept, search auto-queued | `sim/world.ts#reserveSeatRace`, `PassengerConsole.tsx`, `passenger/Contention.tsx` | unit and e2e |
| Progressive fare panel, ৳52.50 = 30.00 + 45.00 − 30% | `passenger/FarePanel.tsx`, `domain/fare.ts` | `fare.test.ts`, e2e |
| Integer poysha everywhere | `domain/money.ts` (refuses non-integers) | `money.test.ts` |
| Driver pipeline: Stop 1–4 with Confirm Arrival → Start Trip → Complete Stop / Ride; 56 px controls; slide to confirm boarding | `driver/DriverStops.tsx` | e2e |
| 48 × 48 touch targets, primary action in the bottom third | sticky `ActionDock`; `min-h-touch` / `min-w-touch` | e2e measures every control's client rect |
| ARIA live regions (polite / assertive), `prefers-reduced-motion` | `a11y/Announcer.tsx`, `MotionConfig reducedMotion="user"`, `globals.css` | axe-core e2e |
| Spring physics 400 / 30 | `design/tokens.ts#spring` → `MotionConfig` | — |
| PRD cast only (no "User 1") | `domain/cast.ts` (ids match the identity seed) | e2e |

## Where this deliberately differs from the guideline

1. **Fare model.** The guideline takes 30 % off the *subtotal* (`(Base + Distance) × (1 − d)`). The backend fare service takes 20 % off the *distance charge* only. The demo tariff follows the guideline: ৳30 base, ৳22.50/km, 30 % pool discount. On the real 2.0 km Banani → Gulshan 1 distance, that gives exactly 3,000 + 4,500 − 2,250 = **5,250 poysha**. A live-API version must render the server's breakdown instead of computing one; the panel already takes any `FareBreakdown`.
2. **`--text-tertiary` (#64748B) fails AA.** The guideline says "> 4.5:1". Measured, it's 4.0:1 on the canvas and 3.0:1 on elevated cards. So it's used only for disabled controls, which WCAG exempts. Three tokens were added for the same reason: `--danger-coral-text` (#F87171), because raw coral text on elevated surfaces is 3.8:1, plus `--danger-coral-subtle` and `--warning-amber-subtle`. The contrast test enforces all of this.
3. **Disabled = `cursor: not-allowed` + clicks blocked in the handler.** The guideline also asks for `pointer-events: none`, but that would hide the not-allowed cursor. `aria-disabled` keeps the control focusable, so its reason (`aria-describedby`) can still be read.
4. **Cancelling after arrival.** The guideline says the passenger has a "restricted window". The backend allows only the driver to cancel after arrival. Here the passenger gets a 60 s grace period. After that, only Jashim can release the seat, as a no-show.
5. **Who wins seat 3.** In the seeded story Nusrat already holds seat 1, so her winning claim (TX-201) books seat 3 for a companion on her ride. That matches the guideline's "Occupied (Nusrat)". Shirin's toast doesn't name the winner.
6. **Lifecycle naming.** The backend's `STARTED` is `IN_TRANSIT` here.
7. **Data source.** The consoles run against an in-browser dispatch model. It ports the backend's rules: same-pickup pooling, 140 % detour cap (`planner.py`), first rider needs acceptance, later riders auto-join, and the zone distances. Nothing calls the gateway yet.
8. **Lighthouse** isn't automated. axe-core runs in the e2e suite.
