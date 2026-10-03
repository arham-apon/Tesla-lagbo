/**
 * Records the Tesla-Lagbo walkthrough video (1920x1080, 30 fps, H.264) from the running production build.
 *
 *   cd frontend && npm run build && npx next start --port 3100
 *   FFMPEG=<path to ffmpeg.exe> node demo-video/record.cjs
 *
 * Output: demo-video/tesla-lagbo-walkthrough.mp4 plus captions (.srt) and a timed narration script (.md).
 * The app is driven through its real UI with real mouse events; captions and the chapter panel sit in the
 * empty side gutters so they never cover the product.
 */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { chromium } = require(path.join(__dirname, '../frontend/node_modules/playwright-core'));

const APP = process.env.APP_URL || 'http://localhost:3100';
const FFMPEG = process.env.FFMPEG;
const OUT = path.join(__dirname, process.env.OUT_NAME || 'tesla-lagbo-walkthrough');
const FPS = 30;
const W = 1920;
const H = 1080;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nowS = () => Date.now() / 1000;

async function main() {
  // ---- browser + screencast -> ffmpeg (constant frame rate from frame timestamps) ----------------------------------
  const browser = await chromium.launch({ channel: 'chrome', args: ['--hide-scrollbars'] });
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, reducedMotion: 'no-preference' });
  const page = await ctx.newPage();
  await page.goto(APP, { waitUntil: 'networkidle' });
  await page.addStyleTag({ url: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap' }).catch(() => {});
  await page.addScriptTag({ path: path.join(__dirname, 'overlay.js') });
  await page.evaluate(() => document.fonts.ready);

  const ff = spawn(FFMPEG, ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'mjpeg', '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', `${OUT}.mp4`], { stdio: ['pipe', 'inherit', 'inherit'] });
  const cdp = await ctx.newCDPSession(page);
  let t0 = null;
  let written = 0;
  let last = null;
  const pump = (untilS) => {
    const target = Math.round((untilS - t0) * FPS);
    while (last && written < target) { ff.stdin.write(last); written++; }
  };
  cdp.on('Page.screencastFrame', async ({ data, metadata, sessionId }) => {
    const ts = metadata.timestamp;
    if (t0 === null) t0 = ts;
    pump(ts);
    last = Buffer.from(data, 'base64');
    cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
  });

  // ---- presentation helpers ---------------------------------------------------------------------------------------
  const log = [];
  let cur = { x: 960, y: 700 };
  const vo = (fn, ...args) => page.evaluate(([f, a]) => window.__vo[f](...a), [fn, args]);
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  async function moveTo(x, y, ms) {
    const from = { ...cur };
    const dist = Math.hypot(x - from.x, y - from.y);
    const dur = ms ?? Math.min(1100, Math.max(350, dist * 0.9));
    const start = Date.now();
    for (;;) {
      const t = Math.min(1, (Date.now() - start) / dur);
      const e = ease(t);
      await page.mouse.move(from.x + (x - from.x) * e, from.y + (y - from.y) * e);
      if (t >= 1) break;
      await sleep(12);
    }
    cur = { x, y };
  }
  async function box(loc) {
    await loc.waitFor({ state: 'visible', timeout: 20000 });
    await loc.scrollIntoViewIfNeeded();
    return loc.boundingBox();
  }
  async function hover(loc, pause = 700, dx = 0.5, dy = 0.5) {
    const b = await box(loc).catch((e) => { console.warn('hover skipped:', String(e).split(String.fromCharCode(10))[0]); return null; });
    if (!b) return;
    await moveTo(b.x + b.width * dx, b.y + b.height * dy);
    await sleep(pause);
  }
  async function click(loc, pause = 900) {
    const b = await box(loc);
    await moveTo(b.x + b.width / 2, b.y + b.height / 2);
    await sleep(250);
    await page.mouse.down();
    await sleep(90);
    await page.mouse.up();
    await sleep(pause);
  }
  async function ring(loc) {
    if (!loc) return vo('ring', null);
    const b = await box(loc).catch((e) => { console.warn('ring skipped:', String(e).split(String.fromCharCode(10))[0]); return null; });
    return vo('ring', b);
  }
  async function ringDeck(sel) {
    const b = await vo('deckRect', sel);
    await vo('ring', b);
    return b;
  }
  const words = (s) => s.split(/\s+/).filter(Boolean).length;
  /** One narrated beat: caption shown, actions run, then hold until the caption has had time to be read. */
  async function beat(who, text, actions, extra = 0) {
    const start = nowS();
    await vo('caption', text, who);
    if (actions) await actions();
    const need = words(text) / 2.7 + 1.0 + extra;
    const spent = nowS() - start;
    if (spent < need) await sleep((need - spent) * 1000);
    log.push({ start, end: nowS(), who, text });
  }

  // cast tracker states
  const cast = {
    Jashim: ['Drives Bullet · 3 seats', ''],
    Nusrat: ['Banani → Mohakhali', ''],
    Rafiq: ['Banani → Gulshan 1', ''],
    Shirin: ['Banani → Gulshan 2', ''],
  };
  const setCast = (id, text, tone = '', active = id) => { if (id) cast[id] = [text, tone]; return vo('cast', cast, active); };

  const tab = (name) => page.getByRole('tab', { name });
  const persona = (name) => page.getByRole('radio', { name: new RegExp(`^${name}`) });

  // ---- slides as a shadow-DOM layer (no navigation, so the screencast never restarts) -----------------------------
  const slideSrc = fs.readFileSync(path.join(__dirname, 'slides.html'), 'utf8');
  let slideCss = slideSrc.match(/<style>([\s\S]*?)<\/style>/)[1];
  slideCss = slideCss.replace(':root {', '.deck {').replace('html, body {', '.deck {').replace('body::before', '.deck::before') + '\n.deck { position: fixed; inset: 0; }';
  const slideHtml = slideSrc.match(/<body>([\s\S]*?)<script>/)[1];

  // ---- start recording ------------------------------------------------------------------------------------------
  await vo('chapter', 1, 'Problem, users & idea');
  await setCast(null, '', '', null);
  await vo('showCast', true);
  await vo('caption', '');
  await page.mouse.move(cur.x, cur.y);
  await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
  await sleep(400);

  // ===================================== PART 1 · 0:00–1:00 =====================================================
  await vo('full', `<div class="vo-title"><div class="k">Final project · Software engineering</div><h1>Tesla-Lagbo</h1>
    <h2>Dhaka Tesla Pool: share a seat, split the fare, survive Dhaka traffic</h2>
    <div class="tag"><span class="vo-chip"><b>Jashim</b> · driver</span><span class="vo-chip"><b>Nusrat</b> · passenger</span><span class="vo-chip"><b>Rafiq</b> · passenger</span><span class="vo-chip"><b>Shirin</b> · the last seat</span></div></div>`);
  await beat('', 'Tesla-Lagbo, or Dhaka Tesla Pool, is a ride-pooling platform for small three-seat electric Teslas.', null, 2.5);
  await vo('full', null);
  await sleep(600);

  const cabinPanel = page.locator('section[aria-labelledby="cabin-title"]');
  const seat3 = page.getByText(/^SEAT\s*03$/).first();
  const manifest = page.getByText('Passenger manifest').locator('xpath=ancestor::section[1]');
  await beat('The problem', 'On a corridor like Banani to Mohakhali, a three-seat Tesla often drives with one rider. Seats are wasted, fares stay high, and the next rider waits for another car.', async () => {
    await hover(cabinPanel, 500, 0.3, 0.1);
    await ring(cabinPanel);
    await sleep(1500);
    await hover(seat3, 900);
  });
  await ring(null);

  await beat('The idea', 'Tesla-Lagbo fills those seats. Riders leaving from the same zone, whose drop-offs fit on one route, share one Tesla. Each pays for their own trip, with a pool discount.', async () => {
    const map = page.getByRole('complementary', { name: 'Live map' });
    await ring(map);
    await hover(map, 1200, 0.5, 0.62);
    await ring(null);
    await hover(page.getByText(/Shares Bullet's route/).first(), 800, 0.3);
  });

  await setCast('Nusrat', 'Passenger · Banani → Mohakhali', 'ok', 'Nusrat');
  await beat('The users', 'There are two kinds of user. Passengers, like Nusrat and Rafiq, who share this corridor, and Shirin, who wants the final seat.', async () => {
    await hover(persona('Nusrat'), 1100, 0.3);
    await setCast('Rafiq', 'Passenger · Banani → Gulshan 1', 'ok', 'Rafiq');
    await hover(persona('Rafiq'), 1100, 0.3);
    await setCast('Shirin', 'Passenger · wants the last seat', 'bad', 'Shirin');
    await hover(persona('Shirin'), 900, 0.3);
  });

  await setCast('Jashim', 'Driver · Bullet, 3-seat Tesla', 'warn', 'Jashim');
  await beat('The users', 'And drivers, like Jashim, who drives Bullet. His cockpit shows the riders, the seats, and one clear next step.', async () => {
    await click(tab(/Driver Cockpit/), 1400);
    await hover(page.getByText('Passenger manifest'), 1200);
  });
  await beat('Core idea', 'The platform manages drivers, riders, seats and the whole ride lifecycle, and it must never sell the same seat twice.', async () => {
    await click(tab(/Passenger Console/), 1000);
    await hover(seat3, 600);
    await ring(cabinPanel);
  });
  await ring(null);

  // ===================================== PART 2 · 1:00–3:00 =====================================================
  await vo('chapter', 2, 'Engineering & architecture');
  await vo('showCast', false);
  await vo('deck', true, slideHtml, slideCss);
  await vo('slide', 0);
  await moveTo(1250, 900);
  await sleep(700);

  await beat('Architecture', 'Every tap follows one path: the Next.js frontend calls the API gateway, the Trip service writes the database, and an event pushes the new state to passenger and driver.', async () => {
    for (const id of ['#f-user', '#f-fe', '#f-gw', '#f-db', '#f-state', '#f-upd']) {
      const b = await ringDeck(id);
      await moveTo(b.x + b.width * 0.8, b.y + b.height * 0.55, 450);
      await sleep(850);
    }
    await vo('ring', null);
  });

  await vo('slide', 1);
  await beat('Architecture', 'The backend is six FastAPI services, one job each, behind a gateway. HTTP when an answer is needed now, RabbitMQ events for facts like "ride completed", Redis for fast-moving data.', async () => {
    await sleep(600);
    for (const id of ['#a-client', '#a-gw', '#a-trip', '#a-mq', '#a-ns']) {
      const b = await ringDeck(id);
      await moveTo(b.x + b.width * 0.75, b.y + b.height * 0.6, 500);
      await sleep(1100);
    }
    await vo('ring', null);
  });

  await vo('slide', 2);
  await beat('Frontend', 'The frontend is Next.js and TypeScript. Every screen derives from one lifecycle status. The live demo runs on an in-browser model that applies the backend\'s pooling rules.', async () => {
    await sleep(700);
    const b = await ringDeck('#fe-honest');
    await moveTo(b.x + b.width * 0.7, b.y + b.height * 0.5);
  });
  await vo('ring', null);

  await vo('slide', 3);
  await beat('Database', 'Each service owns its database. In trip.db, a pool is one trip of one vehicle, with a capacity and an occupied-seat count. Ride requests belong to a pool, and each status change is audited.', async () => {
    await sleep(600);
    let b = await ringDeck('#t-pools');
    await moveTo(b.x + b.width * 0.6, b.y + b.height * 0.55);
    await sleep(2600);
    b = await ringDeck('#t-rides');
    await moveTo(b.x + b.width * 0.6, b.y + b.height * 0.4);
    await sleep(2000);
    b = await ringDeck('#t-hist');
    await moveTo(b.x + b.width * 0.5, b.y + b.height * 0.6);
  });
  await vo('ring', null);

  await vo('slide', 4);
  await beat('Auth & APIs', 'Identity issues signed JWTs and the gateway checks every call. Each button maps to one endpoint, and booking needs an idempotency key, so a retried tap can\'t book twice.', async () => {
    await sleep(600);
    const b = await ringDeck('#api');
    await moveTo(b.x + b.width * 0.55, b.y + b.height * 0.2);
    await sleep(1500);
    await moveTo(b.x + b.width * 0.55, b.y + b.height * 0.52);
  });
  await vo('ring', null);

  await vo('slide', 5);
  await beat('Lifecycle', 'A pool is created when a driver accepts the first request. Compatible riders join and seats fill up. The first boarding closes it to new riders, and the last drop-off completes it. A transition table decides who may move a ride.', async () => {
    await sleep(600);
    const b = await ringDeck('#tl');
    for (let i = 0; i < 7; i++) { await moveTo(b.x + (b.width / 7) * (i + 0.5), b.y + b.height * 0.55, 380); await sleep(700); }
    const s = await ringDeck('#states');
    await moveTo(s.x + s.width * 0.5, s.y + s.height * 0.5);
  });
  await vo('ring', null);

  await vo('slide', 6);
  await beat('Key decision', 'The key decision: how to stop overbooking. There\'s no read-then-write. One conditional UPDATE succeeds only if the seats still fit and the pool version is unchanged. A CHECK constraint backs it up.', async () => {
    await sleep(600);
    const b = await ringDeck('#code-join');
    await moveTo(b.x + b.width * 0.5, b.y + b.height * 0.55);
    await sleep(3200);
    const r = await ringDeck('#race');
    await moveTo(r.x + r.width * 0.25, r.y + r.height * 0.5);
    await sleep(1400);
    await moveTo(r.x + r.width * 0.75, r.y + r.height * 0.5);
  });
  await vo('ring', null);

  await vo('slide', 7);
  await beat('Trade-off', 'We chose SQLite with one writer per service because it makes that seat check simple and race-free. The trade-off: a service can\'t scale out past one container. Postgres per service is the path to scale.', null, 1);

  await vo('slide', 8);
  await vo('chapter', 3, 'Live scenario');
  await beat('', 'Now let\'s watch one Tesla and four people move through the real app.', null, 0.5);

  // ===================================== PART 3 · 3:00–6:00 =====================================================
  await vo('deck', false);
  await vo('showCast', true);
  for (const k of Object.keys(cast)) cast[k] = ['Waiting', ''];
  cast.Jashim = ['Online · no pool yet', 'warn'];
  await vo('cast', cast, null);
  await sleep(500);

  await beat('Setup', 'We reset the demo to an empty Bullet: Jashim is online at Banani, with no riders.', async () => {
    await click(page.getByRole('button', { name: 'Simulation controls' }), 900);
    await hover(page.getByText('Empty Bullet', { exact: true }), 700);
    await click(page.getByRole('button', { name: 'Load' }).nth(1), 800);
  });

  await setCast('Jashim', 'Online · no pool yet', 'warn', 'Jashim');
  await beat('Jashim · driver', 'Jashim\'s cockpit: online, battery at 78 percent, zero of three seats taken. There\'s no pool yet. His first accepted rider creates it.', async () => {
    await click(tab(/Driver Cockpit/), 1000);
    await hover(page.getByText('Receiving ride requests'), 1000);
    await hover(page.getByText('Cabin capacity'), 900);
  });

  await setCast('Nusrat', 'Booking…', 'warn', 'Nusrat');
  await beat('Nusrat · passenger', 'Nusrat opens the app. She picks Banani to Mohakhali from Dhaka\'s zones, and a solo estimate appears right away.', async () => {
    await click(tab(/Passenger Console/), 900);
    await click(persona('Nusrat'), 900);
    await click(page.getByRole('combobox', { name: 'Drop-off' }), 900);
    await hover(page.getByRole('option', { name: /^Gulshan 1/ }), 400);
    await click(page.getByRole('option', { name: /^Mohakhali/ }), 900);
    await hover(page.getByText(/^Solo estimate/).first(), 700);
  });

  await beat('Nusrat · passenger', 'She reserves seat 1. It\'s held for her while Jashim decides: status Requested.', async () => {
    await click(page.getByRole('button', { name: /^Reserve Seat 1/ }), 1500);
    await setCast('Nusrat', 'Requested · seat 1 held', 'warn', 'Nusrat');
  }, -0.5);

  await setCast('Jashim', 'New ride request', 'warn', 'Jashim');
  await beat('Jashim · driver', 'Jashim gets a 15-second offer with Nusrat\'s route and fare. He accepts, and that creates the pool.', async () => {
    await click(tab(/Driver Cockpit/), 900);
    const accept = page.getByRole('button', { name: /^Accept/ });
    await accept.waitFor({ state: 'visible', timeout: 30000 });
    await ring(page.locator('[aria-labelledby="offer-title"]'));
    await hover(page.getByText(/To Mohakhali Flyover/), 1300);
    await ring(null);
    await click(accept, 1000);
    await setCast('Jashim', 'Pool created · FORMING', 'ok', 'Jashim');
    await setCast('Nusrat', 'Matched · seat 1', 'ok', 'Jashim');
  });

  await beat('Pool created', 'The pool is forming. Nusrat is in seat 1, and his stop list starts with her pickup at Banani Road 11.', async () => {
    await hover(page.getByText('Passenger manifest'), 600);
    await ring(manifest);
    await sleep(1600);
    await ring(null);
    await hover(page.getByText(/Next Stop:/), 900);
  });

  await beat('Nusrat · passenger', 'On Nusrat\'s phone: matched with Jashim, Bullet\'s plate, and a live pickup countdown.', async () => {
    await click(tab(/Passenger Console/), 900);
    await vo('cast', cast, 'Nusrat');
    await ring(page.locator('[aria-labelledby="status-title"]'));
    await hover(page.getByText('DHAKA-TESLA-11'), 1200);
    await hover(page.getByText('Pickup ETA'), 800);
  });
  await ring(null);

  await setCast('Rafiq', 'Looking for a ride', 'warn', 'Rafiq');
  await beat('Rafiq · passenger', 'Rafiq wants Banani to Gulshan 1. He sees the same Tesla, now one of three seats taken, and the app checks his stop keeps every rider within the 140 percent detour cap.', async () => {
    await click(persona('Rafiq'), 1000);
    await ring(cabinPanel);
    await hover(page.getByText(/^SEAT\s*01$/).first(), 1400);
    await ring(null);
    await hover(page.getByText(/Shares Bullet's route/).first(), 1500, 0.3);
  });

  await beat('Rafiq · passenger', 'He reserves seat 2 and joins instantly: later riders don\'t need the driver\'s approval. The pool discount applies to his fare.', async () => {
    await click(page.getByRole('button', { name: /^Reserve Seat 2/ }), 1400);
    await setCast('Rafiq', 'Auto-joined · seat 2', 'ok', 'Rafiq');
    await hover(page.getByText(/^Pool fare/).first(), 1200, 0.3);
  });

  await beat('Shared Tesla', 'Back in Jashim\'s cockpit: two passengers share one Tesla, two of three seats. The planner has ordered the stops: both pickups, then Rafiq\'s drop at Gulshan 1, then Nusrat\'s at Mohakhali.', async () => {
    await click(tab(/Driver Cockpit/), 900);
    await vo('cast', cast, 'Jashim');
    await ring(manifest);
    await sleep(1500);
    await ring(page.getByText('Dispatch queue').locator('xpath=ancestor::section[1]'));
    const q = page.getByText('Dispatch queue');
    await hover(q, 300);
    for (const s of [/Stop 1:/, /Stop 2:/, /Stop 3:/, /Stop 4:/]) await hover(page.getByText(s).first(), 750, 0.2);
  });
  await ring(null);

  await setCast('Shirin', 'Wants the last seat', 'bad', 'Shirin');
  await beat('Shirin · the last seat', 'Now Shirin, going to Gulshan 2. Exactly one seat is left: seat 3.', async () => {
    await click(tab(/Passenger Console/), 900);
    await click(persona('Shirin'), 1000);
    await ring(seat3.locator('xpath=ancestor::*[self::button or self::li or @role="radio"][1]'));
    await hover(seat3, 900);
  });
  await ring(null);

  await beat('Shirin · the last seat', 'To show the edge case, we fire a competing claim for the same seat at the same moment, like two phones tapping together.', async () => {
    await click(page.getByRole('switch', { name: /Simulate Shirin/ }), 1000);
  });

  await beat('Shirin · the last seat', 'Shirin taps Reserve. The other claim reaches the server 340 milliseconds earlier. Her request is refused cleanly, and seat 3 is never sold twice.', async () => {
    await click(page.getByRole('button', { name: /^Reserve Seat 3/ }), 2600);
    await setCast('Shirin', 'Seat taken · queued', 'bad', 'Shirin');
  });

  await beat('What the system did', 'No crash and no overbooking. The cabin now shows three of three, a banner explains what happened, her route is kept, and she\'s queued for the next pooled Tesla automatically.', async () => {
    await ring(page.getByText('Lock conflict handled').locator('xpath=ancestor::*[@role="alert" or contains(@class,"rounded-xl")][1]'));
    await hover(page.getByText('Lock conflict handled'), 1800);
    await ring(null);
    await page.evaluate(() => window.scrollTo({ top: 260, behavior: 'smooth' }));
    await sleep(900);
    await hover(seat3, 1000);
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
    await sleep(700);
    await hover(persona('Shirin'), 500, 0.3);
  });

  await beat('Jashim · driver', 'Jashim\'s manifest agrees: three of three seats, and nobody else can get in.', async () => {
    await click(tab(/Driver Cockpit/), 900);
    await vo('cast', cast, 'Jashim');
    await hover(page.getByText('Cabin capacity'), 900);
    await ring(manifest);
    await sleep(1200);
  });
  await ring(null);

  async function arriveAndBoard() {
    await click(page.getByRole('button', { name: 'Confirm Arrival' }), 1300);
    const handle = page.getByRole('button', { name: /Confirm Passenger Boarding/ });
    const hb = await box(handle);
    const track = await handle.locator('xpath=..').boundingBox();
    await moveTo(hb.x + hb.width / 2, hb.y + hb.height / 2);
    await page.mouse.down();
    await moveTo(track.x + track.width - 20, hb.y + hb.height / 2, 900);
    await page.mouse.up();
    await sleep(1300);
  }
  await beat('Ride in progress', 'The ride begins. Jashim confirms arrival, then slides to board each passenger, so a bump in traffic can\'t start a trip by accident. The first boarding closes the pool to new riders.', async () => {
    await arriveAndBoard();
    await setCast('Nusrat', 'On board · In transit', 'ok', 'Jashim');
    await setCast('Jashim', 'Pool IN_PROGRESS', 'ok', 'Jashim');
    await arriveAndBoard();
    await setCast('Rafiq', 'On board · In transit', 'ok', 'Jashim');
  });

  await beat('Ride completes', 'He drops Rafiq at Gulshan 1, then Nusrat at Mohakhali. Each drop-off settles that fare into his ledger, in integer poysha.', async () => {
    await click(page.getByRole('button', { name: /^Complete Stop/ }), 1500);
    await setCast('Rafiq', 'Completed · fare settled', 'ok', 'Jashim');
    await hover(page.getByText('Settled today (poysha ledger)'), 900);
    await click(page.getByRole('button', { name: /^Complete Ride/ }), 1200);
    await setCast('Nusrat', 'Completed · fare settled', 'ok', 'Jashim');
  });

  await beat('Shirin · the last seat', 'The pool closes and the seats free up. Shirin\'s queued request goes straight to Jashim as the next offer, and he accepts.', async () => {
    const accept = page.getByRole('button', { name: /^Accept/ });
    await accept.waitFor({ state: 'visible', timeout: 30000 });
    await setCast('Jashim', 'Pool COMPLETED · next offer', 'ok', 'Shirin');
    await hover(page.getByText(/^Shirin ·/).first(), 1400);
    await click(accept, 1000);
    await setCast('Shirin', 'Matched · next trip', 'ok', 'Shirin');
  });

  await beat('Trip history', 'Jashim\'s trip history keeps the finished pool: both riders, their routes and the fares he earned.', async () => {
    await click(page.getByRole('tab', { name: /Trip history/ }), 1200);
    await ring(page.getByRole('tab', { name: /Trip history/ }).locator('xpath=ancestor::section[1]'));
    await sleep(1400);
  });
  await ring(null);

  // Recap card
  await moveTo(1760, 1010, 800);
  await vo('caption', '');
  await vo('full', `<div class="vo-recap"><div class="k">Recap</div><h1>One Tesla, four people, no overbooking</h1>
    <div class="vo-rr"><div class="vo-av d">J</div><b>Jashim</b><span>Accepted the first request, which <em>created the pool</em>, then drove it to completion</span></div>
    <div class="vo-rr"><div class="vo-av p">N</div><b>Nusrat</b><span>Requested seat 1 and was <em>matched</em> when Jashim accepted</span></div>
    <div class="vo-rr"><div class="vo-av p">R</div><b>Rafiq</b><span><em>Auto-joined</em> seat 2 within the 140% detour cap, with the pool discount</span></div>
    <div class="vo-rr"><div class="vo-av s">S</div><b>Shirin</b><span>Lost the race for seat 3 <em>without an oversell</em>, was queued, and got the next trip</span></div></div>`);
  const recapStart = nowS();
  for (let i = 1; i <= 4; i++) { await sleep(1300); await page.evaluate((i) => document.querySelectorAll('.vo-rr')[i - 1].classList.add('on'), i); }
  await sleep(9000);
  log.push({ start: recapStart, end: nowS(), who: 'Recap', text: 'Jashim created the pool. Nusrat joined. Rafiq auto-joined. Shirin tried for the last seat: refused without overbooking, queued, and matched on the next trip.' });
  await vo('full', null);

  await beat('', 'That\'s Tesla-Lagbo: shared Tesla rides where every seat is sold exactly once. Thanks for watching!', async () => {
    await click(tab(/Passenger Console/), 800);
    await moveTo(1100, 980, 900);
  }, 1.5);

  // ---- stop + flush -----------------------------------------------------------------------------------------------
  await cdp.send('Page.stopScreencast');
  pump(nowS());
  ff.stdin.end();
  await new Promise((r) => ff.on('close', r));
  await browser.close();

  // captions + script
  const fmt = (s) => {
    const ms = Math.max(0, Math.round((s - t0) * 1000));
    const h = String(Math.floor(ms / 3600000)).padStart(2, '0');
    const m = String(Math.floor(ms / 60000) % 60).padStart(2, '0');
    const sec = String(Math.floor(ms / 1000) % 60).padStart(2, '0');
    return `${h}:${m}:${sec},${String(ms % 1000).padStart(3, '0')}`;
  };
  fs.writeFileSync(`${OUT}.srt`, log.map((b, i) => `${i + 1}\n${fmt(b.start)} --> ${fmt(b.end)}\n${b.text}\n`).join('\n'));
  const mmss = (s) => { const x = Math.max(0, s - t0); return `${Math.floor(x / 60)}:${String(Math.floor(x % 60)).padStart(2, '0')}`; };
  fs.writeFileSync(`${OUT}-narration.md`, `# Tesla-Lagbo walkthrough: timed narration\n\nRead these over the video (≈140 wpm). Times match ${path.basename(OUT)}.mp4.\n\n| Time | Beat | Narration |\n|---|---|---|\n` +
    log.map((b) => `| ${mmss(b.start)}–${mmss(b.end)} | ${b.who || '—'} | ${b.text.replace(/\|/g, '\\|')} |`).join('\n') + '\n');
  console.log(`video length ≈ ${(written / FPS).toFixed(1)} s, frames ${written}`);
}

main().catch((e) => { console.error(e); process.exit(1); });
