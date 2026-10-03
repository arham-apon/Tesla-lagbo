// Presentation layer drawn on top of the running app during recording: cursor, click ripple,
// chapter panel (left gutter), narration captions (right gutter), title/recap cards, attention ring,
// and a shadow-DOM host for the engineering slides. Nothing here changes the app's behaviour.
(() => {
  if (window.__vo) return;
  const css = `
    #vo-root { position: fixed; inset: 0; z-index: 2147483000; pointer-events: none; font-family: Inter, "Segoe UI", system-ui, sans-serif; color: #F9FAFB; }
    #vo-cursor { position: fixed; left: 0; top: 0; width: 30px; height: 30px; z-index: 2147483647; transform: translate(-100px, -100px); filter: drop-shadow(0 3px 6px rgba(0,0,0,.6)); }
    .vo-ripple { position: fixed; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 999px; border: 3px solid #F59E0B; z-index: 2147483646; animation: vo-rip .6s ease-out forwards; }
    @keyframes vo-rip { from { transform: scale(.4); opacity: 1; } to { transform: scale(3.2); opacity: 0; } }
    #vo-left { position: fixed; left: 22px; top: 104px; width: 284px; display: flex; flex-direction: column; gap: 14px; transition: opacity .5s; }
    .vo-card { background: rgba(17,24,39,.94); border: 1px solid rgba(255,255,255,.14); border-radius: 14px; padding: 16px 18px; box-shadow: 0 8px 28px -4px rgba(0,0,0,.6); }
    .vo-part { font-size: 13px; letter-spacing: .14em; text-transform: uppercase; color: #10B981; font-weight: 700; }
    .vo-ptitle { font-size: 21px; font-weight: 800; margin-top: 4px; line-height: 1.2; }
    .vo-prog { display: flex; gap: 6px; margin-top: 12px; }
    .vo-prog i { flex: 1; height: 4px; border-radius: 4px; background: rgba(255,255,255,.14); }
    .vo-prog i.on { background: #10B981; }
    .vo-prog i.cur { background: #F59E0B; }
    .vo-plabels { display: flex; justify-content: space-between; margin-top: 6px; font-size: 11px; color: #94A3B8; }
    .vo-h { font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: #94A3B8; font-weight: 700; margin-bottom: 10px; }
    .vo-cast { display: flex; flex-direction: column; gap: 8px; }
    .vo-row { display: grid; grid-template-columns: 34px 1fr; gap: 10px; align-items: center; padding: 8px 8px; border-radius: 10px; border: 1px solid transparent; transition: all .35s; }
    .vo-row.active { border-color: #F59E0B; background: rgba(245,158,11,.10); }
    .vo-av { width: 34px; height: 34px; border-radius: 9px; display: grid; place-items: center; font-weight: 800; font-size: 15px; }
    .vo-av.d { background: rgba(245,158,11,.16); color: #F59E0B; }
    .vo-av.p { background: rgba(16,185,129,.14); color: #10B981; }
    .vo-av.s { background: rgba(239,68,68,.14); color: #F87171; }
    .vo-nm { font-size: 16px; font-weight: 700; display: flex; justify-content: space-between; gap: 6px; }
    .vo-nm small { font-weight: 600; font-size: 11px; color: #94A3B8; text-transform: uppercase; letter-spacing: .08em; }
    .vo-st { font-size: 13px; color: #94A3B8; margin-top: 1px; }
    .vo-st.ok { color: #10B981; } .vo-st.warn { color: #F59E0B; } .vo-st.bad { color: #F87171; }
    #vo-cap { position: fixed; right: 22px; top: 404px; width: 290px; transition: opacity .35s; }
    #vo-cap .vo-card { border-left: 4px solid #10B981; padding: 18px 20px; }
    #vo-cap p { font-size: 19px; line-height: 1.5; color: #F1F5F9; }
    #vo-cap .vo-who { font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: #10B981; font-weight: 700; margin-bottom: 8px; }
    #vo-ring { position: fixed; border: 3px solid #F59E0B; border-radius: 16px; box-shadow: 0 0 0 6px rgba(245,158,11,.18); opacity: 0; transition: all .45s cubic-bezier(.2,.8,.2,1); }
    #vo-full { position: fixed; inset: 0; display: grid; place-items: center; background: rgba(11,15,23,.72); backdrop-filter: blur(10px); opacity: 0; transition: opacity .8s; }
    #vo-full.on { opacity: 1; }
    .vo-title { text-align: center; line-height: 1.3; }
    .vo-title h1, .vo-recap h1 { line-height: 1.1; }
    .vo-title h2 { line-height: 1.35; }
    .vo-rr span { line-height: 1.4; }
    .vo-recap .k, .vo-title .k { line-height: 1.6; }
    .vo-title .k { font-size: 20px; letter-spacing: .3em; text-transform: uppercase; color: #10B981; font-weight: 700; }
    .vo-title h1 { font-size: 112px; font-weight: 800; letter-spacing: -.02em; margin: 14px 0 6px; }
    .vo-title h2 { font-size: 34px; font-weight: 500; color: #94A3B8; }
    .vo-title .tag { margin-top: 36px; display: inline-flex; gap: 14px; }
    .vo-chip { border: 1px solid rgba(255,255,255,.22); border-radius: 999px; padding: 10px 20px; font-size: 20px; color: #CBD5E1; background: rgba(30,41,59,.8); }
    .vo-chip b { color: #F9FAFB; }
    .vo-recap { width: 1060px; }
    .vo-recap h1 { font-size: 54px; font-weight: 800; margin-bottom: 26px; text-align: center; }
    .vo-recap .k { text-align: center; font-size: 18px; letter-spacing: .3em; text-transform: uppercase; color: #10B981; font-weight: 700; margin-bottom: 10px; }
    .vo-rr { display: grid; grid-template-columns: 64px 180px 1fr; gap: 18px; align-items: center; padding: 18px 24px; margin-bottom: 12px; border-radius: 14px; background: rgba(17,24,39,.95); border: 1px solid rgba(255,255,255,.14); opacity: 0; transform: translateY(12px); transition: all .5s; }
    .vo-rr.on { opacity: 1; transform: none; }
    .vo-rr .vo-av { width: 56px; height: 56px; font-size: 24px; border-radius: 14px; }
    .vo-rr b { font-size: 28px; }
    .vo-rr span { font-size: 22px; color: #CBD5E1; }
    .vo-rr em { font-style: normal; color: #10B981; font-weight: 700; }
    #vo-deck { position: fixed; inset: 0; opacity: 0; transition: opacity .7s; background: #0B0F17; }
    #vo-deck.on { opacity: 1; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const root = document.createElement('div');
  root.id = 'vo-root';
  root.innerHTML = `
    <div id="vo-deck"></div>
    <div id="vo-ring"></div>
    <div id="vo-left">
      <div class="vo-card" id="vo-chapter"><div class="vo-part"></div><div class="vo-ptitle"></div>
        <div class="vo-prog"><i></i><i></i><i></i></div>
        <div class="vo-plabels"><span>Idea</span><span>Engineering</span><span>Live demo</span></div></div>
      <div class="vo-card" id="vo-castcard"><div class="vo-h">The Tesla pool</div><div class="vo-cast" id="vo-cast"></div></div>
    </div>
    <div id="vo-cap"><div class="vo-card"><div class="vo-who"></div><p></p></div></div>
    <div id="vo-full"></div>
    <svg id="vo-cursor" viewBox="0 0 24 24"><path d="M4 2 L4 20 L8.6 15.6 L11.6 22 L14.6 20.7 L11.7 14.5 L18 14.5 Z" fill="#F9FAFB" stroke="#0B0F17" stroke-width="1.4" stroke-linejoin="round"/></svg>
  `;
  document.body.appendChild(root);
  const $ = (s) => root.querySelector(s);

  const cursor = $('#vo-cursor');
  document.addEventListener('mousemove', (e) => { cursor.style.transform = `translate(${e.clientX - 5}px, ${e.clientY - 3}px)`; }, true);
  document.addEventListener('mousedown', (e) => {
    const r = document.createElement('div');
    r.className = 'vo-ripple';
    r.style.left = e.clientX + 'px';
    r.style.top = e.clientY + 'px';
    root.appendChild(r);
    setTimeout(() => r.remove(), 700);
  }, true);

  const CAST = [
    { id: 'Jashim', cls: 'd', role: 'Driver' },
    { id: 'Nusrat', cls: 'p', role: 'Passenger' },
    { id: 'Rafiq', cls: 'p', role: 'Passenger' },
    { id: 'Shirin', cls: 's', role: 'Passenger' },
  ];

  window.__vo = {
    chapter(part, title) {
      $('#vo-chapter .vo-part').textContent = `Part ${part} of 3`;
      $('#vo-chapter .vo-ptitle').textContent = title;
      root.querySelectorAll('.vo-prog i').forEach((el, i) => { el.className = i + 1 < part ? 'on' : i + 1 === part ? 'cur' : ''; });
    },
    cast(state, active) {
      $('#vo-cast').innerHTML = CAST.map((c) => {
        const s = state[c.id] || ['', ''];
        return `<div class="vo-row ${active === c.id ? 'active' : ''}"><div class="vo-av ${c.cls}">${c.id[0]}</div>
          <div><div class="vo-nm">${c.id}<small>${c.role}</small></div><div class="vo-st ${s[1]}">${s[0]}</div></div></div>`;
      }).join('');
    },
    showCast(on) { $('#vo-castcard').style.display = on ? '' : 'none'; },
    caption(text, who) {
      const cap = $('#vo-cap');
      if (!text) { cap.style.opacity = 0; return; }
      cap.style.opacity = 1;
      $('#vo-cap .vo-who').textContent = who || '';
      $('#vo-cap .vo-who').style.display = who ? '' : 'none';
      $('#vo-cap p').textContent = text;
    },
    ring(rect) {
      const r = $('#vo-ring');
      if (!rect) { r.style.opacity = 0; return; }
      const pad = 8;
      Object.assign(r.style, { left: rect.x - pad + 'px', top: rect.y - pad + 'px', width: rect.width + pad * 2 + 'px', height: rect.height + pad * 2 + 'px', opacity: 1 });
    },
    full(html) {
      const f = $('#vo-full');
      if (!html) { f.classList.remove('on'); return; }
      f.innerHTML = html;
      requestAnimationFrame(() => f.classList.add('on'));
    },
    reveal(sel) { root.querySelectorAll(sel).forEach((e) => e.classList.add('on')); },
    deck(on, html, css) {
      const host = $('#vo-deck');
      if (html && !host.shadowRoot) {
        const sr = host.attachShadow({ mode: 'open' });
        sr.innerHTML = `<style>${css}</style><div class="deck">${html}</div>`;
      }
      host.classList.toggle('on', !!on);
    },
    slide(i) { const sr = $('#vo-deck').shadowRoot; sr.querySelectorAll('.slide').forEach((s, k) => s.classList.toggle('on', k === i)); },
    deckRect(sel) { const e = $('#vo-deck').shadowRoot.querySelector(sel); const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; },
    hideLeft(on) { $('#vo-left').style.opacity = on ? 0 : 1; },
  };
})();
