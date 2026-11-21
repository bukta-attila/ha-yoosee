const AHEAD_S = 60;
const BEHIND_S = 30;
const ZV = [86400, 3600, 600, 60];
const ZK = [10, 30, 60, 300, 600, 1800, 3600, 10800];

const pad = n => String(n).padStart(2, '0');
const dkey = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const dadd = (d, n) => { const x = new Date(d + 'T12:00:00'); x.setDate(x.getDate() + n); return dkey(x); };
const sec = s => { const [h, m, x] = s.split(':').map(Number); return h * 3600 + m * 60 + x; };
const hms = v => `${pad(Math.floor(v / 3600))}:${pad(Math.floor(v / 60) % 60)}:${pad(Math.floor(v) % 60)}`;
const hm = v => `${pad(Math.floor(v / 3600))}:${pad(Math.floor(v / 60) % 60)}`;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const nowSec = () => { const d = new Date(); return d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds(); };
const esc = s => String(s).replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);
const icon = n => `<ha-icon icon="mdi:${n}"></ha-icon>`;
const put = (sb, fn) => new Promise((y, n) => { sb.addEventListener('updateend', y, { once: true }); sb.addEventListener('error', n, { once: true }); fn(); });

const CSS = `
:host{display:block;height:100%;--bg:var(--primary-background-color);--fg:var(--primary-text-color);--mu:var(--secondary-text-color);--sf:var(--secondary-background-color);--ln:var(--divider-color);--ac:var(--primary-color);--acf:var(--text-primary-color,#fff);--sh:var(--card-background-color);--bad:var(--error-color)}
.app{height:100%;display:flex;flex-direction:column;background:var(--bg);color:var(--fg);font-family:var(--ha-font-family-body,var(--paper-font-body1_-_font-family,system-ui,sans-serif));position:relative}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:disabled{cursor:default;opacity:.35}
ha-icon{--mdc-icon-size:22px;display:flex}
.top{display:flex;align-items:center;gap:4px;height:56px;padding:0 4px 0 calc(4px + var(--safe-area-inset-left,0px));flex:none;background:var(--app-header-background-color,var(--bg));color:var(--app-header-text-color,var(--fg));border-bottom:var(--app-header-border-bottom,1px solid var(--ln))}
.top h1{margin:0 0 0 8px;font-size:20px;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top select{font:inherit;font-size:18px;max-width:70%;margin-left:8px;height:40px;padding:0 8px;border-radius:8px;border:0;background:transparent;color:inherit}
.top select option{color:var(--fg);background:var(--sh)}
.scr{flex:1;overflow-y:auto;overscroll-behavior:contain}
.wrap{display:grid;grid-template-columns:1fr;gap:16px;max-width:900px;margin:0 auto;padding:16px 16px calc(24px + var(--safe-area-inset-bottom,0px))}
.rest{display:flex;flex-direction:column;gap:14px;min-width:0}
.stage{position:relative;aspect-ratio:16/9;overflow:hidden;background:#000;border-radius:12px;user-select:none;-webkit-user-select:none}
.stage:fullscreen{aspect-ratio:auto;border-radius:0}
.stage .post,.stage .box,.stage video,.stage .box>*{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block}
.stage .hide{visibility:hidden}
.ov{position:absolute;inset:0;z-index:3}
.fd{transition:opacity .45s cubic-bezier(.2,.8,.2,1)}
.stage.idle .fd{opacity:0;pointer-events:none}
.gls{background:rgba(22,22,22,.42);-webkit-backdrop-filter:blur(16px) saturate(1.5);backdrop-filter:blur(16px) saturate(1.5);border:1px solid rgba(255,255,255,.14);color:#f1f1f1}
.tl{position:absolute;top:10px;left:10px;display:flex;gap:6px}
.chp{height:26px;padding:0 10px 0 9px;border-radius:13px;display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;letter-spacing:.09em;white-space:nowrap;text-transform:uppercase}
.chp.tm{letter-spacing:.01em;font-size:12px;font-weight:500;font-variant-numeric:tabular-nums;text-transform:none}
.chp i{width:6px;height:6px;border-radius:50%;background:var(--bad);animation:lp 2.2s ease-in-out infinite}
@keyframes lp{0%,100%{opacity:1}50%{opacity:.35}}
.ld{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);display:flex;align-items:center;gap:9px;height:36px;padding:0 15px 0 12px;border-radius:18px;font-size:13px;white-space:nowrap}
.ld ha-icon,.live ha-icon{--mdc-icon-size:16px}
.spin{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.live{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);height:34px;padding:0 14px 0 11px;border-radius:17px;display:flex;align-items:center;gap:7px;font-size:13px;font-weight:500;white-space:nowrap}
.fsx{display:none;position:absolute;top:16px;right:16px;width:40px;height:40px;border-radius:50%;place-items:center}
.stage:fullscreen .fsx{display:grid}
.flash{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;z-index:4}
.flash.go{animation:fl .55s ease-out}
@keyframes fl{0%{opacity:.85}100%{opacity:0}}
.cbar{display:flex;justify-content:space-around;margin-top:-6px}
.cbar button{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;color:var(--ac);transition:transform .2s,background .2s}
.cbar ha-icon{--mdc-icon-size:24px}
.cbar button:active{transform:scale(.9)}
.cbar button.on{background:var(--ac);color:var(--acf)}
.gh{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:4px 0 -2px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mu);font-weight:500}
.daybtn{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 13px;border-radius:17px;background:var(--sf);color:var(--fg);font-size:13px;text-transform:none;letter-spacing:0}
.daybtn.on{background:var(--ac);color:var(--acf)}
.daybtn ha-icon{--mdc-icon-size:18px}
.zt{position:relative;height:56px;border-radius:10px;background:var(--sf);overflow:hidden;--zw:0}
.zq{position:absolute;inset:0;overflow-x:scroll;overflow-y:hidden;touch-action:pan-x;scrollbar-width:none;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;user-select:none;-webkit-user-select:none;cursor:grab}
.zq::-webkit-scrollbar{display:none}
.zq:active{cursor:grabbing}
.zw{display:flex;width:max-content;height:100%}
.zp{flex:none;width:50vw}
.zs{position:relative;flex:none;height:100%;width:calc(24px * var(--zw))}
.zs>.rrbox,.zs>.zk{position:absolute;inset:0}
.zs .zk{pointer-events:none}
.zs .rr{position:absolute;top:20px;bottom:0;background:color-mix(in srgb,var(--fg) 22%,transparent);min-width:2px}
.zs .zk i{position:absolute;bottom:0;width:1px;height:6px;background:var(--mu)}
.zs .zk i.b{height:12px}
.zs .zk .zn{position:absolute;top:4px;transform:translateX(-50%);font-size:10px;color:var(--mu);font-variant-numeric:tabular-nums}
.zc{position:absolute;left:50%;top:0;bottom:0;width:2px;margin-left:-1px;background:var(--ac);box-shadow:0 0 0 1px rgba(0,0,0,.4);pointer-events:none}
.zl{position:absolute;left:50%;top:2px;transform:translateX(-50%);padding:1px 6px;border-radius:6px;background:var(--fg);color:var(--bg);font-size:11px;font-variant-numeric:tabular-nums;z-index:1;pointer-events:none}
.seek{display:flex;justify-content:center;align-items:center;gap:10px}
.seek button{display:inline-flex;align-items:center;justify-content:center;width:46px;height:38px;border-radius:19px;background:var(--sf);color:var(--fg)}
.seek button:active{transform:scale(.94)}
.seek ha-icon{--mdc-icon-size:20px}
.hint{font-size:13px;color:var(--mu);text-align:center;margin:0}
.hint:empty{display:none}
.shade.dcal{background:transparent;display:block;animation:fd .15s}
.dcalbox{position:fixed;width:320px;max-width:calc(100vw - 24px);max-height:76vh;overflow:auto;background:var(--sh);border-radius:16px;padding:14px 16px;display:flex;flex-direction:column;gap:12px;box-shadow:0 12px 44px rgba(0,0,0,.5);animation:pop .16s ease}
@keyframes pop{from{opacity:0;transform:scale(.96)}}
.shade{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:5;display:flex;justify-content:center;align-items:flex-end;animation:fd .28s}
.sheet{width:100%;max-width:480px;background:var(--sh);border-radius:24px 24px 0 0;padding:10px 20px calc(22px + var(--safe-area-inset-bottom,0px));display:flex;flex-direction:column;gap:16px;max-height:88%;overflow:auto;animation:up .36s cubic-bezier(.32,.72,0,1)}
@keyframes fd{from{opacity:0}}
@keyframes up{from{transform:translateY(100%)}}
.grab{width:40px;height:4px;border-radius:2px;background:var(--ln);margin:0 auto}
.sheet h3{margin:0;font-size:18px;font-weight:500;text-align:center}
.pmh{display:flex;align-items:center;justify-content:space-between}
.pmh b{font-weight:500;text-transform:capitalize}
.pmh button{width:40px;height:40px;border-radius:50%;display:grid;place-items:center}
.pcal{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.pcal .dn{font-size:11px;color:var(--mu);text-align:center;padding-bottom:4px;text-transform:capitalize}
.pcal button{aspect-ratio:1;border-radius:50%;display:grid;place-items:center;font-variant-numeric:tabular-nums;font-size:15px}
.pcal button.td{color:var(--ac);font-weight:600}
.pcal button.on{background:var(--ac);color:var(--acf)}
.toast{position:fixed;left:50%;bottom:calc(24px + var(--safe-area-inset-bottom,0px));transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:10px 16px;border-radius:20px;font-size:14px;z-index:6;max-width:90%;text-align:center}
@media (max-width:600px){.wrap{padding-top:0}.stage{margin:0 -16px;border-radius:0}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
`;

class YooseePanel extends HTMLElement {
  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (this._menu) this._menu.hass = h;
    if (first) this.setup();
  }
  set narrow(n) { this._narrow = n; if (this._menu) this._menu.narrow = n; }
  set panel(p) { }

  t(k) { return this.L?.[k] ?? k; }
  ws(msg) { return this._hass.callWS({ ...msg, entry_id: this.c.entry_id, device_id: this.c.device_id }); }

  async lang() {
    const l = (this._hass.locale?.language || this._hass.language || 'en').toLowerCase(), v = new URL(import.meta.url).search;
    for (const x of [l, l.split('-')[0], 'en']) {
      try { const r = await fetch(new URL(`i18n/${x}.json${v}`, import.meta.url)); if (r.ok) return r.json(); } catch (e) { }
    }
    return {};
  }

  async setup() {
    this.L = await this.lang();
    this.fmt = (o, d) => new Intl.DateTimeFormat(this._hass.locale?.language || this._hass.language, o).format(d);
    const t = k => this.t(k);
    const r = this.attachShadow({ mode: 'open' });
    r.innerHTML = `<style>${CSS}</style><div class="app"><div class="top"><span class="mb"></span><h1></h1><select hidden></select></div>
      <div class="scr"><div class="wrap"><div class="stage"><img class="post hide" alt=""><div class="box"><video class="lv" playsinline autoplay muted></video><video class="rv hide" playsinline muted></video></div><div class="ov"></div><div class="flash"></div></div>
      <div class="rest"><div class="cbar"></div>
        <div class="gh"><span>${t('playback')}</span><button class="daybtn" data-a="cal"></button></div>
        <div class="zt"><div class="zq"><div class="zw"><i class="zp"></i><div class="zs"><div class="rrbox"></div><div class="zk"></div></div><i class="zp"></i></div></div><i class="zc"></i><span class="zl"></span></div>
        <div class="seek"><button data-a="seek:-10" aria-label="${t('seek_back')}">${icon('rewind-10')}</button><button class="z" data-a="zoom:-1" aria-label="${t('zoom_out')}">${icon('magnify-minus-outline')}</button><button class="z" data-a="zoom:1" aria-label="${t('zoom_in')}">${icon('magnify-plus-outline')}</button><button data-a="seek:10" aria-label="${t('seek_fwd')}">${icon('fast-forward-10')}</button></div>
        <p class="hint"></p><div class="calin"></div></div></div></div>
      <div class="sh"></div><div class="toast" hidden></div></div>`;
    const $ = s => r.querySelector(s);
    Object.assign(this, { scr: $('.scr'), h1: $('h1'), csel: $('select'), stage: $('.stage'), post: $('.post'), box: $('.box'), lv: $('.lv'), rv: $('.rv'), ov: $('.ov'), rest: $('.rest'), sh: $('.sh'), tst: $('.toast'), cbar: $('.cbar'), daybtn: $('.daybtn'), zt: $('.zt'), zq: $('.zq'), zw: $('.zw'), zs: $('.zs'), rrbox: $('.rrbox'), zk: $('.zk'), zl: $('.zl'), hintEl: $('.hint'), calin: $('.calin') });
    this.zIn = r.querySelector('[data-a="zoom:1"]');
    this.zOut = r.querySelector('[data-a="zoom:-1"]');
    this.Z = { w: 0, prog: null, f: false, u: false, raf: null, pn: null, t0: null, m: null, kr: null, c: null, st: null };
    this.desktop = window.matchMedia('(any-pointer: fine)').matches; this.zDay = null;
    if (customElements.get('ha-menu-button')) {
      this._menu = document.createElement('ha-menu-button');
      Object.assign(this._menu, { hass: this._hass, narrow: this._narrow });
      $('.mb').append(this._menu);
    }
    r.addEventListener('click', e => this.click(e));
    this.stage.addEventListener('pointerdown', () => this.wake());
    this.csel.addEventListener('change', () => this.use(this.cams[+this.csel.value]));
    this.lv.addEventListener('playing', () => { clearTimeout(this.lto); if (!this.liveOk) { this.liveOk = true; this.tries = 0; this.render(); } });
    const rv = this.rv;
    rv.addEventListener('timeupdate', () => { const p = this.rec; if (!p || p.load) return; p.t = p.b + rv.currentTime; const tm = this.ov.querySelector('.chp.tm'); if (tm) tm.textContent = this.tm(p); });
    rv.addEventListener('waiting', () => { const p = this.rec; if (p && !p.load && !p.done) { p.wait = true; this.render(); } });
    rv.addEventListener('playing', () => { const p = this.rec; if (p?.wait) { p.wait = false; this.render(); } });
    rv.addEventListener('ended', () => { const p = this.rec, s = this.D[p?.day]?.segs || [], i = s.findIndex(x => x[2] === p?.name), n = s[i + 1]; if (n && n[0] - s[i][1] < 5) this.play(n[0], p.day); });
    document.addEventListener('visibilitychange', () => (document.hidden ? this.stopLive() : this.sync()));
    this.zBind();
    new ResizeObserver(() => this.zResize()).observe(this.scr);
    this.cams = (await this._hass.callWS({ type: 'yoosee_cloud/cameras' })).cameras;
    this.csel.replaceChildren(...this.cams.map((c, i) => new Option(c.name, i)));
    this.csel.hidden = this.cams.length < 2;
    this.h1.hidden = !this.csel.hidden;
    this.use(this.cams[0]);
  }

  connectedCallback() { if (this.c) this.sync(); }
  disconnectedCallback() { this.toLive(true); this.stopLive(); }

  use(c) {
    if (!c) return;
    this.toLive(true);
    this.stopLive();
    Object.assign(this, { c, D: {}, dl: {}, first: null, day: dkey(new Date()), liveErr: null, cal: null, tries: 0, zDay: null });
    if (this.Z) this.Z.c = null;
    this.h1.textContent = c.name;
    this.post.classList.add('hide');
    this.snap();
    this.ws({ type: 'yoosee_cloud/days' }).then(r => { if (this.c === c && r.days?.length) { this.first = [...r.days].sort()[0]; this.render(); } }).catch(() => { });
    this.sync();
  }

  sync() {
    if (!this.c || !this.isConnected || document.hidden) return;
    if (!this.rec && !this.pc && !this.alt && !this.liveErr) this.live();
    const D = this.D[this.day];
    if (!this.dl[this.day] && (!D || (this.day === dkey(new Date()) && Date.now() - D.at > 60000))) this.load(this.day);
    this.render();
  }

  async load(day) {
    const c = this.c, D = { segs: [], at: Date.now() };
    this.dl[day] = true;
    try {
      D.segs = (await this.ws({ type: 'yoosee_cloud/recordings', date: day })).segs.map(s => { const a = sec(s[0]), b = sec(s[1]); return [a, b > a ? b : 86400, s[2]]; }).sort((a, b) => a[0] - b[0]);
    } catch (e) { D.err = e?.code === 'login_unavailable' ? 'login' : 'load_err'; }
    if (this.c !== c) return;
    this.D[day] = D;
    delete this.dl[day];
    if (this.day === day) { if (day !== dkey(new Date())) this.zDay = null; this.render(); }
  }

  async snap() {
    const c = this.c, ent = c.live_camera;
    if (!ent || !this._hass.states[ent]) return;
    try {
      const r = await this._hass.fetchWithAuth(`/api/camera_proxy/${ent}?t=${Date.now()}`);
      if (!r.ok || this.c !== c) return;
      const old = this.post.src;
      this.post.src = URL.createObjectURL(await r.blob());
      this.post.classList.remove('hide');
      if (old) URL.revokeObjectURL(old);
    } catch (e) { }
  }

  async live() {
    const ent = this.c.live_camera, st = ent && this._hass.states[ent], h = this._hass;
    if (!st) { this.liveErr = 'no_live'; return this.render(); }
    if (this.busy) return;
    this.busy = true;
    let conf = null;
    try { conf = (await h.callWS({ type: 'camera/webrtc/get_client_config', entity_id: ent })).configuration || {}; } catch (e) { }
    this.busy = false;
    if (this.pc || this.alt || this.rec || !this.isConnected || document.hidden || ent !== this.c.live_camera) return;
    if (!conf) return this.altLive(ent, st);
    const pc = this.pc = new RTCPeerConnection(conf), q = [], ms = new MediaStream();
    let sid = null;
    this.liveOk = false;
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });
    pc.ontrack = e => { ms.addTrack(e.track); if (this.lv.srcObject !== ms) this.lv.srcObject = ms; this.lv.play().catch(() => { }); };
    const send = c => h.callWS({ type: 'camera/webrtc/candidate', entity_id: ent, session_id: sid, candidate: c }).catch(() => { });
    pc.onicecandidate = e => { if (!e.candidate) return; const c = e.candidate.toJSON(); if (sid) send(c); else q.push(c); };
    pc.onconnectionstatechange = () => { if (pc.connectionState === 'failed' && this.pc === pc) this.liveFail(); };
    clearTimeout(this.lto);
    this.lto = setTimeout(() => { if (this.pc === pc && !this.liveOk) this.liveFail(); }, 20000);
    try {
      const off = await pc.createOffer();
      await pc.setLocalDescription(off);
      const un = await h.connection.subscribeMessage(m => {
        if (this.pc !== pc) return;
        if (m.type === 'session') { sid = m.session_id; q.splice(0).forEach(send); }
        else if (m.type === 'answer') pc.setRemoteDescription({ type: 'answer', sdp: m.answer }).catch(() => this.liveFail());
        else if (m.type === 'candidate') pc.addIceCandidate(m.candidate).catch(() => { });
        else if (m.type === 'error') this.liveFail();
      }, { type: 'camera/webrtc/offer', entity_id: ent, offer: off.sdp });
      if (this.pc === pc) this.unsub = un; else un();
    } catch (e) { if (this.pc === pc) this.liveFail(); }
    this.render();
  }

  altLive(ent, st) {
    let el;
    if (customElements.get('ha-camera-stream')) {
      el = document.createElement('ha-camera-stream');
      Object.assign(el, { hass: this._hass, stateObj: st, muted: !this.un, controls: false });
    } else {
      el = document.createElement('img');
      el.src = `/api/camera_proxy_stream/${ent}?token=${st.attributes.access_token}`;
    }
    this.alt = el;
    this.lv.classList.add('hide');
    this.box.append(el);
    this.liveOk = true;
    this.render();
  }

  stopLive() {
    const pc = this.pc;
    this.pc = null;
    this.liveOk = false;
    clearTimeout(this.lto);
    if (this.unsub) { try { this.unsub(); } catch (e) { } this.unsub = null; }
    if (pc) pc.close();
    if (this.lv) this.lv.srcObject = null;
    if (this.alt) { this.alt.remove(); this.alt = null; this.lv.classList.remove('hide'); }
  }

  liveFail() {
    this.stopLive();
    if ((this.tries = (this.tries || 0) + 1) < 3) { setTimeout(() => this.sync(), 3000); return this.render(); }
    this.liveErr = 'live_err';
    this.render();
  }

  abort() {
    if (this.ab) { this.ab.abort(); this.ab = null; }
    if (!this.rv) return;
    this.rv.pause();
    this.rv.removeAttribute('src');
    this.rv.load();
    if (this.rurl) { URL.revokeObjectURL(this.rurl); this.rurl = null; }
  }

  toLive(quiet) {
    const was = !!this.rec;
    this.abort();
    this.rec = null;
    this.liveErr = null;
    this.rv?.classList.add('hide');
    if (was && this.c) this.ws({ type: 'yoosee_cloud/stop' }).catch(() => { });
    if (!quiet) this.sync();
  }

  async play(t, day = this.day) {
    const D = this.D[day];
    if (!D) return;
    const seg = D.segs.find(s => t >= s[0] && t < s[1]) || D.segs.find(s => s[0] >= t);
    if (!seg) return this.toast(this.t('no_rec_at'));
    const off = Math.max(0, t - seg[0]), rv = this.rv;
    this.stopLive();
    this.abort();
    const rec = this.rec = { name: seg[2], b: seg[0], t: seg[0] + off, day, load: true, pct: 0 };
    rv.muted = !this.un;
    rv.classList.remove('hide');
    this.render();
    const ac = this.ab = new AbortController();
    const ready = () => { if (this.rec !== rec || !rec.load) return; rec.load = false; this.render(); };
    try {
      const { url } = await this.ws({ type: 'yoosee_cloud/clip', name: seg[2], offset: Math.floor(off) });
      const res = await fetch(url, { signal: ac.signal });
      if (!res.ok) throw new Error();
      const mime = `video/mp4; codecs="${res.headers.get('X-Codecs') || 'avc1.42E01E'}"`;
      const MS = window.ManagedMediaSource || window.MediaSource;
      if (!MS || !MS.isTypeSupported(mime)) {
        const b = await res.blob();
        if (ac.signal.aborted) return;
        this.rurl = URL.createObjectURL(b);
        rv.src = this.rurl;
        rv.addEventListener('loadedmetadata', () => { rv.currentTime = off; rv.play().catch(() => { }); ready(); }, { once: true });
        return;
      }
      const ms = new MS();
      rv.disableRemotePlayback = true;
      this.rurl = URL.createObjectURL(ms);
      rv.src = this.rurl;
      await new Promise(y => ms.addEventListener('sourceopen', y, { once: true }));
      const sb = ms.addSourceBuffer(mime), rd = res.body.getReader();
      let pend = [], len = 0, go = false;
      const flush = async () => {
        if (!len) return;
        const buf = new Uint8Array(len);
        let o = 0;
        for (const p of pend) { buf.set(p, o); o += p.length; }
        pend = []; len = 0;
        await put(sb, () => sb.appendBuffer(buf));
        const st = sb.buffered.length ? sb.buffered.start(0) : 0, end = sb.buffered.length ? sb.buffered.end(sb.buffered.length - 1) : 0;
        if (!go && end > Math.max(off, st) + 1.5) { go = true; rv.currentTime = Math.max(off, st); rv.play().catch(() => { }); ready(); }
      };
      for (;;) {
        const { done, value } = await rd.read();
        if (ac.signal.aborted) return;
        if (done) break;
        pend.push(value); len += value.length;
        if (len >= 262144 || !go) await flush();
        if (go && sb.buffered.length) {
          const ct = rv.currentTime;
          if (ct - sb.buffered.start(0) > BEHIND_S + 10) await put(sb, () => sb.remove(0, ct - BEHIND_S));
          while (sb.buffered.length && sb.buffered.end(sb.buffered.length - 1) - rv.currentTime > AHEAD_S) {
            await new Promise(y => setTimeout(y, 500));
            if (ac.signal.aborted || this.rec !== rec) return;
          }
        }
      }
      await flush();
      rec.done = true;
      if (ms.readyState === 'open') ms.endOfStream();
      if (!go && !sb.buffered.length) throw new Error();
      if (!go) { rv.currentTime = sb.buffered.start(0); rv.play().catch(() => { }); ready(); }
    } catch (e) {
      if (ac.signal.aborted || this.rec !== rec) return;
      this.toast(this.t('play_err'), 4000);
      this.toLive();
    }
  }

  dayLb(day) {
    const T = dkey(new Date());
    if (day === T) return this.t('today');
    if (day === dadd(T, -1)) return this.t('yesterday');
    return this.fmt({ month: 'short', day: 'numeric' }, new Date(day + 'T12:00:00'));
  }

  tm(p) { return `${this.dayLb(p.day)} ${hms(p.t)}`; }

  render() {
    if (!this.c || !this.ov) return;
    const t = k => this.t(k), rec = this.rec, D = this.D[this.day], T = dkey(new Date()), day = this.day;
    const ld = rec ? (rec.load || rec.wait ? t('loading') : '') : !this.liveOk && !this.liveErr ? t('connecting') : '';
    this.stage.classList.toggle('idle', !!this.idle && !ld);
    this.ov.innerHTML = `<div class="tl fd">${rec ? `<span class="chp gls tm">${this.tm(rec)}</span>` : this.liveOk ? `<span class="chp gls"><i></i>${t('live')}</span>` : ''}</div>
      ${ld ? `<div class="ld gls"><span class="spin"></span>${ld}</div>` : ''}
      ${!rec && this.liveErr === 'live_err' ? `<button class="ld gls" data-a="retry">${icon('refresh')}${t('live_err')}</button>` : ''}
      ${!rec && this.liveErr === 'no_live' ? `<div class="ld gls">${t('no_live')}</div>` : ''}
      ${rec ? `<button class="live gls fd" data-a="live">${icon('play')}${t('back_live')}</button>` : ''}
      <button class="fsx gls" data-a="fs" aria-label="${t('exit_fullscreen')}">${icon('fullscreen-exit')}</button>`;
    this.cbar.innerHTML = `<button data-a="snd" class="${this.un ? 'on' : ''}" aria-label="${t('sound')}" aria-pressed="${!!this.un}">${icon(this.un ? 'volume-high' : 'volume-off')}</button>
      <button data-a="shot" aria-label="${t('snapshot')}">${icon('camera')}</button><button data-a="fs" aria-label="${t('fullscreen')}">${icon('fullscreen')}</button>`;
    this.daybtn.className = 'daybtn' + (this.cal ? ' on' : '');
    this.daybtn.innerHTML = `${icon('calendar-month')}<span>${day === T ? t('calendar') : esc(this.dayLb(day))}</span>`;
    this.hintEl.textContent = D?.err ? t(D.err) : (D && !D.segs.length ? t('no_rec') : '');
    const cw = this.zq.clientWidth;
    this.zOut.disabled = !!cw && this.Z.w > 0 && this.Z.w <= cw / 24 * 1.01;
    this.zIn.disabled = !!cw && this.Z.w >= cw * 60 * 0.99;
    this.zDraw();
    this.zInit();
    const calInner = this.cal ? this.calHtml() : '';
    if (this.desktop) {
      this.calin.innerHTML = '';
      this.sh.innerHTML = this.cal ? `<div class="shade dcal" data-a="close"><div class="dcalbox"><h3 style="margin:0;font-size:15px;font-weight:500;text-align:center">${t('pick_day')}</h3>${calInner}</div></div>` : '';
      if (this.cal) this.posPop();
    } else {
      this.calin.innerHTML = '';
      this.sh.innerHTML = this.cal ? `<div class="shade" data-a="close"><div class="sheet"><div class="grab"></div><h3>${t('pick_day')}</h3>${calInner}</div></div>` : '';
    }
  }

  zpw() { return this.zw.firstElementChild.offsetWidth; }
  zx(x) { return this.zpw() - this.zq.clientWidth / 2 + x / 3600 * this.Z.w; }
  zat(dx = 0) { return clamp((this.zq.scrollLeft + this.zq.clientWidth / 2 + dx - this.zpw()) / this.Z.w * 3600, 0, this.maxT()); }
  maxT(day = this.day) { return day === dkey(new Date()) ? nowSec() : 86399; }
  zCap() { if (!this.Z.w || this.day !== dkey(new Date())) return; const mx = this.zx(this.maxT()); if (this.zq.scrollLeft > mx) this.zq.scrollLeft = mx; }

  zDef() {
    if (this.Z.c != null) return this.Z.c;
    if (this.day === dkey(new Date())) return nowSec();
    const segs = this.D[this.day]?.segs || [];
    return segs.length ? segs[segs.length - 1][1] : 43200;
  }

  zDraw() {
    const segs = this.D[this.day]?.segs || [];
    this.rrbox.innerHTML = segs.map(s => `<i class="rr" style="left:calc(${(s[0] / 3600).toFixed(6)}px * var(--zw));width:calc(${(Math.max(0, s[1] - s[0]) / 3600).toFixed(6)}px * var(--zw))"></i>`).join('');
    this.zTicks(true);
  }

  zTicks(force) {
    const cw = this.zq.clientWidth;
    if (!cw || !this.Z.w) return;
    const a = (this.zq.scrollLeft - this.zpw()) / this.Z.w * 3600, sp = cw / this.Z.w * 3600;
    const lab = ZK.find(k => k / 3600 * this.Z.w >= 64) || 10800, mn = [...ZK].reverse().find(k => k < lab && lab % k === 0 && k / 3600 * this.Z.w >= 8) || lab;
    const from = Math.max(0, Math.floor((a - sp) / lab) * lab), to = Math.min(86400, a + 2 * sp), key = `${this.Z.w}|${this.day}|${from}`;
    if (!force && this.Z.kr === key) return;
    this.Z.kr = key;
    let out = '';
    for (let x = from; x <= to; x += mn) {
      const l = (x / 3600 * this.Z.w).toFixed(1), b = x % lab === 0;
      out += `<i class="${b ? 'b' : ''}" style="left:${l}px"></i>`;
      if (b) out += `<span class="zn" style="left:${l}px">${x >= 86400 ? '24:00' : lab < 60 ? hms(x) : hm(x)}</span>`;
    }
    this.zk.innerHTML = out;
  }

  zGo(x) {
    if (!this.zq.clientWidth || !this.Z.w) return;
    this.zq.scrollLeft = this.zx(x);
    this.Z.prog = this.zq.scrollLeft;
    this.Z.c = x;
    this.zl.textContent = hms(x);
    this.zTicks();
  }

  zSetW(w, x, px) {
    const cw = this.zq.clientWidth;
    if (!cw) return;
    this.Z.w = clamp(w, cw / 24, cw * 60);
    this.zt.style.setProperty('--zw', this.Z.w);
    this.zq.scrollLeft = this.zpw() + x / 3600 * this.Z.w - (px ?? cw / 2);
    this.Z.prog = this.zq.scrollLeft;
    this.Z.c = this.zat();
    this.zl.textContent = hms(this.Z.c);
    this.zTicks(true);
  }

  zInit() {
    if (this.Z.f || this.Z.u || !this.zq.clientWidth) return;
    if (this.zDay === this.day) return;
    if (this.day !== dkey(new Date()) && !this.D[this.day]) return;
    if (!this.Z.w) this.zSetW(this.zq.clientWidth * 6, this.zDef()); else this.zGo(this.zDef());
    this.zDay = this.day;
  }

  zResize() {
    if (!this.c || !this.zq.clientWidth) return;
    if (!this.Z.w) return this.zInit();
    this.zSetW(this.Z.w, this.zat());
  }

  segAt(x) {
    const segs = this.D[this.day]?.segs || [];
    if (!segs.length) return null;
    const inside = segs.find(s => x >= s[0] && x < s[1]);
    if (inside) return x;
    let best = segs[0][0], bd = Infinity;
    for (const s of segs) for (const e of [s[0], Math.max(s[0], s[1] - 1)]) { const d = Math.abs(e - x); if (d < bd) { bd = d; best = e; } }
    return best;
  }

  zPick(x) { const tt = this.segAt(x); if (tt == null) return; this.zGo(tt); this.play(tt, this.day); }
  zSettle() { if (this.Z.f || !this.Z.u) return; this.Z.u = false; this.zPick(this.zat()); }
  zArm() { clearTimeout(this.Z.st); this.Z.st = setTimeout(() => this.zSettle(), 250); }
  zTap(cx) { const r = this.zq.getBoundingClientRect(); this.Z.u = false; clearTimeout(this.Z.st); this.zPick(this.zat(cx - (r.left + r.width / 2))); }

  zStep(dir) {
    const cw = this.zq.clientWidth;
    if (!cw || !this.Z.w) return;
    const v = cw * 3600 / this.Z.w, n = dir > 0 ? ZV.find(x => x < v * 0.95) ?? ZV[ZV.length - 1] : [...ZV].reverse().find(x => x > v * 1.05) ?? ZV[0];
    this.zSetW(cw * 3600 / n, this.zat());
    this.render();
  }

  seekBy(d) {
    if (this.rec) this.play(clamp((this.rec.t || 0) + d, 0, this.maxT(this.rec.day)), this.rec.day);
    else if (this.Z.w) this.zGo(clamp(this.zat() + d, 0, this.maxT()));
  }

  posPop() {
    const box = this.sh.querySelector('.dcalbox');
    if (!box) return;
    const b = this.daybtn.getBoundingClientRect(), vw = innerWidth, vh = innerHeight, gap = 8, m = 12;
    const w = box.offsetWidth, h = box.offsetHeight;
    const below = vh - b.bottom - gap, above = b.top - gap;
    let top = (h <= below || below >= above) ? b.bottom + gap : b.top - gap - h;
    box.style.top = clamp(top, m, vh - h - m) + 'px';
    box.style.left = clamp(b.right - w, m, vw - w - m) + 'px';
  }

  zBind() {
    const zq = this.zq;
    zq.addEventListener('scroll', () => {
      if (this.Z.pn) return;
      this.zCap();
      if (!this.Z.f && Math.abs(zq.scrollLeft - (this.Z.prog ?? -1)) < 1) return;
      this.Z.u = true; this.Z.prog = null;
      if (!this.Z.raf) this.Z.raf = requestAnimationFrame(() => { this.Z.raf = null; if (!this.Z.w) return; const x = this.zat(); this.Z.c = x; this.zl.textContent = hms(x); this.zTicks(); });
      this.zArm();
    }, { passive: true });
    zq.addEventListener('touchstart', e => {
      this.Z.f = true;
      if (e.touches.length === 2) {
        const [a, b] = e.touches, r = zq.getBoundingClientRect(), mx = (a.clientX + b.clientX) / 2 - r.left;
        this.Z.pn = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1, w: this.Z.w, x: this.zat(mx - r.width / 2) };
        this.Z.t0 = null; this.Z.u = false; clearTimeout(this.Z.st);
        return;
      }
      const p = e.touches[0];
      this.Z.t0 = { x: p.clientX, y: p.clientY, sl: zq.scrollLeft, n: e.touches.length };
    }, { passive: true });
    zq.addEventListener('touchmove', e => {
      const P = this.Z.pn;
      if (!P || e.touches.length < 2) return;
      const [a, b] = e.touches, r = zq.getBoundingClientRect();
      this.zSetW(P.w * (Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1) / P.d, P.x, (a.clientX + b.clientX) / 2 - r.left);
    }, { passive: true });
    const zEnd = e => {
      if (this.Z.pn && e.touches.length < 2) { this.Z.pn = null; this.Z.prog = zq.scrollLeft; }
      if (!this.Z.f || e.touches.length) return;
      this.Z.f = false;
      const d = this.Z.t0, p = e.changedTouches[0];
      this.Z.t0 = null;
      if (e.type === 'touchend' && d && d.n === 1 && !this.Z.u && Math.abs(zq.scrollLeft - d.sl) < 1 && Math.hypot(p.clientX - d.x, p.clientY - d.y) < 8) { this.zTap(p.clientX); return; }
      this.zArm();
    };
    zq.addEventListener('touchend', zEnd);
    zq.addEventListener('touchcancel', zEnd);
    zq.addEventListener('pointerdown', e => {
      if (e.pointerType === 'touch' || e.button !== 0) return;
      try { zq.setPointerCapture(e.pointerId); } catch (x) { }
      this.Z.f = true;
      this.Z.m = { id: e.pointerId, x: e.clientX, sl: zq.scrollLeft, m: false };
    });
    zq.addEventListener('pointermove', e => {
      const d = this.Z.m;
      if (!d || e.pointerId !== d.id) return;
      const dx = e.clientX - d.x;
      if (Math.abs(dx) > 4) d.m = true;
      if (d.m) { zq.scrollLeft = d.sl - dx; this.zCap(); }
    });
    const zUp = e => {
      const d = this.Z.m;
      if (!d || e.pointerId !== d.id) return;
      this.Z.m = null; this.Z.f = false;
      if (e.type === 'pointerup' && !d.m) { this.zTap(e.clientX); return; }
      this.zArm();
    };
    zq.addEventListener('pointerup', zUp);
    zq.addEventListener('pointercancel', zUp);
    zq.addEventListener('wheel', e => {
      if (!e.ctrlKey || !this.Z.w) return;
      e.preventDefault();
      const r = zq.getBoundingClientRect(), x = e.clientX - r.left;
      this.zSetW(this.Z.w * Math.exp(-e.deltaY * 0.01), this.zat(x - r.width / 2), x);
      this.render();
    }, { passive: false });
  }

  calHtml() {
    const T = dkey(new Date()), first = this.first || dadd(T, -14), m = this.cal, d0 = new Date(m + '-01T12:00:00');
    const nd = new Date(d0.getFullYear(), d0.getMonth() + 1, 0).getDate(), off = (d0.getDay() + 6) % 7;
    let c = [...Array(7)].map((_, i) => `<span class="dn">${this.fmt({ weekday: 'narrow' }, new Date(2024, 0, 1 + i))}</span>`).join('') + '<span></span>'.repeat(off);
    for (let d = 1; d <= nd; d++) { const k = `${m}-${pad(d)}`; c += `<button class="${k === T ? 'td' : ''} ${k === this.day ? 'on' : ''}" data-a="day:${k}" ${k >= first && k <= T ? '' : 'disabled'}>${d}</button>`; }
    return `<div class="pmh"><button data-a="m:-1" ${m <= first.slice(0, 7) ? 'disabled' : ''} aria-label="${this.t('prev_month')}">${icon('chevron-left')}</button><b>${esc(this.fmt({ month: 'long', year: 'numeric' }, d0))}</b><button data-a="m:1" ${m >= T.slice(0, 7) ? 'disabled' : ''} aria-label="${this.t('next_month')}">${icon('chevron-right')}</button></div><div class="pcal">${c}</div>`;
  }

  click(e) {
    const el = e.target.closest('[data-a]');
    if (!el) return;
    if (el.classList.contains('shade') && e.target !== el) return;
    const [a, v] = el.dataset.a.split(/:(.*)/);
    if (a === 'snd') {
      this.un = !this.un;
      this.lv.muted = this.rv.muted = !this.un;
      if (this.alt) this.alt.muted = !this.un;
      if (this.un) this.lv.play().catch(() => { });
      return this.render();
    }
    if (a === 'shot') return this.shot();
    if (a === 'fs') return this.fs();
    if (a === 'live') return this.toLive();
    if (a === 'retry') { this.liveErr = null; this.tries = 0; return this.sync(); }
    if (a === 'seek') return this.seekBy(+v);
    if (a === 'zoom') return this.zStep(+v);
    if (a === 'cal') { this.cal = this.cal ? null : this.day.slice(0, 7); return this.render(); }
    if (a === 'close') { this.cal = null; return this.render(); }
    if (a === 'm') { const d = new Date(this.cal + '-15T12:00:00'); d.setMonth(d.getMonth() + +v); this.cal = dkey(d).slice(0, 7); return this.render(); }
    if (a === 'day') { this.day = v; this.cal = null; this.Z.c = null; this.zDay = null; return this.sync(); }
  }

  wake() {
    this.idle = false;
    this.stage.classList.remove('idle');
    clearTimeout(this.it);
    this.it = setTimeout(() => { this.idle = true; this.render(); }, 3500);
  }

  async shot() {
    const v = this.rec ? this.rv : this.alt ? null : this.lv;
    let blob = null;
    try {
      if (v?.videoWidth) {
        const c = document.createElement('canvas');
        c.width = v.videoWidth; c.height = v.videoHeight;
        c.getContext('2d').drawImage(v, 0, 0);
        blob = await new Promise(y => c.toBlob(y, 'image/jpeg', 0.92));
      } else {
        const r = await this._hass.fetchWithAuth(`/api/camera_proxy/${this.c.live_camera}?t=${Date.now()}`);
        if (r.ok) blob = await r.blob();
      }
    } catch (e) { }
    if (!blob) return this.toast(this.t('snap_err'));
    const fl = this.shadowRoot.querySelector('.flash');
    fl.classList.remove('go'); void fl.offsetWidth; fl.classList.add('go');
    const d = new Date(), name = `${this.c.name}-${dkey(d)}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.jpg`.replace(/[\\/:*?"<>|\s]+/g, '_');
    const file = new File([blob], name, { type: 'image/jpeg' });
    if (navigator.canShare?.({ files: [file] })) { try { await navigator.share({ files: [file] }); return; } catch (e) { if (e?.name === 'AbortError') return; } }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    this.toast(this.t('snap_ok'));
  }

  fs() {
    const v = this.rec ? this.rv : this.lv;
    if (document.fullscreenElement) return document.exitFullscreen();
    if (this.stage.requestFullscreen) this.stage.requestFullscreen().then(() => screen.orientation?.lock?.('landscape').catch(() => { })).catch(() => v.webkitEnterFullscreen?.());
    else v.webkitEnterFullscreen?.();
  }

  toast(s, ms = 2500) {
    this.tst.textContent = s;
    this.tst.hidden = false;
    clearTimeout(this.tt);
    this.tt = setTimeout(() => { this.tst.hidden = true; }, ms);
  }
}

customElements.define('yoosee-cloud-panel', YooseePanel);
