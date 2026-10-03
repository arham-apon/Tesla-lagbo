# Tesla-Lagbo walkthrough: timed narration

Read these over the video (≈140 wpm). Times match tesla-lagbo-walkthrough.mp4.

| Time | Beat | Narration |
|---|---|---|
| 0:00–0:09 | — | Tesla-Lagbo, or Dhaka Tesla Pool, is a ride-pooling platform for small three-seat electric Teslas. |
| 0:09–0:21 | The problem | On a corridor like Banani to Mohakhali, a three-seat Tesla often drives with one rider. Seats are wasted, fares stay high, and the next rider waits for another car. |
| 0:21–0:33 | The idea | Tesla-Lagbo fills those seats. Riders leaving from the same zone, whose drop-offs fit on one route, share one Tesla. Each pays for their own trip, with a pool discount. |
| 0:33–0:42 | The users | There are two kinds of user. Passengers, like Nusrat and Rafiq, who share this corridor, and Shirin, who wants the final seat. |
| 0:42–0:50 | The users | And drivers, like Jashim, who drives Bullet. His cockpit shows the riders, the seats, and one clear next step. |
| 0:50–0:58 | Core idea | The platform manages drivers, riders, seats and the whole ride lifecycle, and it must never sell the same seat twice. |
| 1:00–1:11 | Architecture | Every tap follows one path: the Next.js frontend calls the API gateway, the Trip service writes the database, and an event pushes the new state to passenger and driver. |
| 1:11–1:23 | Architecture | The backend is six FastAPI services, one job each, behind a gateway. HTTP when an answer is needed now, RabbitMQ events for facts like "ride completed", Redis for fast-moving data. |
| 1:23–1:34 | Frontend | The frontend is Next.js and TypeScript. Every screen derives from one lifecycle status. The live demo runs on an in-browser model that applies the backend's pooling rules. |
| 1:34–1:48 | Database | Each service owns its database. In trip.db, a pool is one trip of one vehicle, with a capacity and an occupied-seat count. Ride requests belong to a pool, and each status change is audited. |
| 1:48–2:00 | Auth & APIs | Identity issues signed JWTs and the gateway checks every call. Each button maps to one endpoint, and booking needs an idempotency key, so a retried tap can't book twice. |
| 2:00–2:16 | Lifecycle | A pool is created when a driver accepts the first request. Compatible riders join and seats fill up. The first boarding closes it to new riders, and the last drop-off completes it. A transition table decides who may move a ride. |
| 2:16–2:29 | Key decision | The key decision: how to stop overbooking. There's no read-then-write. One conditional UPDATE succeeds only if the seats still fit and the pool version is unchanged. A CHECK constraint backs it up. |
| 2:29–2:44 | Trade-off | We chose SQLite with one writer per service because it makes that seat check simple and race-free. The trade-off: a service can't scale out past one container. Postgres per service is the path to scale. |
| 2:44–2:50 | — | Now let's watch one Tesla and four people move through the real app. |
| 2:51–2:58 | Setup | We reset the demo to an empty Bullet: Jashim is online at Banani, with no riders. |
| 2:58–3:07 | Jashim · driver | Jashim's cockpit: online, battery at 78 percent, zero of three seats taken. There's no pool yet. His first accepted rider creates it. |
| 3:07–3:17 | Nusrat · passenger | Nusrat opens the app. She picks Banani to Mohakhali from Dhaka's zones, and a solo estimate appears right away. |
| 3:17–3:22 | Nusrat · passenger | She reserves seat 1. It's held for her while Jashim decides: status Requested. |
| 3:22–3:30 | Jashim · driver | Jashim gets a 15-second offer with Nusrat's route and fare. He accepts, and that creates the pool. |
| 3:30–3:38 | Pool created | The pool is forming. Nusrat is in seat 1, and his stop list starts with her pickup at Banani Road 11. |
| 3:38–3:44 | Nusrat · passenger | On Nusrat's phone: matched with Jashim, Bullet's plate, and a live pickup countdown. |
| 3:44–3:57 | Rafiq · passenger | Rafiq wants Banani to Gulshan 1. He sees the same Tesla, now one of three seats taken, and the app checks his stop keeps every rider within the 140 percent detour cap. |
| 3:57–4:06 | Rafiq · passenger | He reserves seat 2 and joins instantly: later riders don't need the driver's approval. The pool discount applies to his fare. |
| 4:06–4:18 | Shared Tesla | Back in Jashim's cockpit: two passengers share one Tesla, two of three seats. The planner has ordered the stops: both pickups, then Rafiq's drop at Gulshan 1, then Nusrat's at Mohakhali. |
| 4:18–4:24 | Shirin · the last seat | Now Shirin, going to Gulshan 2. Exactly one seat is left: seat 3. |
| 4:24–4:34 | Shirin · the last seat | To show the edge case, we fire a competing claim for the same seat at the same moment, like two phones tapping together. |
| 4:34–4:44 | Shirin · the last seat | Shirin taps Reserve. The other claim reaches the server 340 milliseconds earlier. Her request is refused cleanly, and seat 3 is never sold twice. |
| 4:44–4:56 | What the system did | No crash and no overbooking. The cabin now shows three of three, a banner explains what happened, her route is kept, and she's queued for the next pooled Tesla automatically. |
| 4:56–5:02 | Jashim · driver | Jashim's manifest agrees: three of three seats, and nobody else can get in. |
| 5:02–5:14 | Ride in progress | The ride begins. Jashim confirms arrival, then slides to board each passenger, so a bump in traffic can't start a trip by accident. The first boarding closes the pool to new riders. |
| 5:14–5:23 | Ride completes | He drops Rafiq at Gulshan 1, then Nusrat at Mohakhali. Each drop-off settles that fare into his ledger, in integer poysha. |
| 5:23–5:32 | Shirin · the last seat | The pool closes and the seats free up. Shirin's queued request goes straight to Jashim as the next offer, and he accepts. |
| 5:32–5:39 | Trip history | Jashim's trip history keeps the finished pool: both riders, their routes and the fares he earned. |
| 5:40–5:54 | Recap | Jashim created the pool. Nusrat joined. Rafiq auto-joined. Shirin tried for the last seat: refused without overbooking, queued, and matched on the next trip. |
| 5:54–6:02 | — | That's Tesla-Lagbo: shared Tesla rides where every seat is sold exactly once. Thanks for watching! |
