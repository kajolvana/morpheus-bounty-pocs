// hud.js — everything the player reads: health, ammo, timer, score,
// money, killfeed, radio, buy menu, plant/defuse bars, scope overlay and
// the minimap. The minimap redraws at 8 Hz, never every frame.
import { GAME, WEAPONS, SITES } from './config.js';

export class HUD {
  constructor(doc) {
    this.doc = doc;
    const $ = (id) => doc.getElementById(id);
    this.el = {
      health: $('health-num'), healthBar: $('health-bar'), armor: $('armor-num'),
      ammoMag: $('ammo-mag'), ammoReserve: $('ammo-reserve'), weaponName: $('weapon-name'),
      timer: $('timer'), phase: $('phase-label'), score: $('score-line'),
      money: $('money'), kills: $('kills'), round: $('round-label'),
      killfeed: $('killfeed'), radio: $('radio-feed'), banner: $('banner'),
      bannerTitle: $('banner-title'), bannerSub: $('banner-sub'),
      buyMenu: $('buy-menu'), buyMoney: $('buy-money'),
      plantBar: $('plant-bar'), plantFill: $('plant-fill'), plantLabel: $('plant-label'),
      scope: $('scope'), flash: $('flash'), minimap: $('minimap'),
      crosshair: $('crosshair'), hitmarker: $('hitmarker'),
      grenades: $('grenades'), hint: $('hint'),
    };
    this.minimapCtx = this.el.minimap.getContext('2d');
    this.lastMinimap = 0;
    this.radioLines = [];
    this.feedLines = [];
  }

  showBanner(title, sub, ms = 2600) {
    this.el.bannerTitle.textContent = title;
    this.el.bannerSub.textContent = sub || '';
    this.el.banner.hidden = false;
    clearTimeout(this._bannerT);
    this._bannerT = setTimeout(() => { this.el.banner.hidden = true; }, ms);
  }

  killfeedLine(text, cls) {
    const d = this.doc.createElement('div');
    d.className = 'feed-line ' + (cls || '');
    d.textContent = text;
    this.el.killfeed.prepend(d);
    while (this.el.killfeed.children.length > 5) this.el.killfeed.lastChild.remove();
    setTimeout(() => d.classList.add('fade'), 4200);
  }

  radioLine(text) {
    const d = this.doc.createElement('div');
    d.className = 'radio-line';
    d.textContent = '▸ ' + text;
    this.el.radio.prepend(d);
    while (this.el.radio.children.length > 4) this.el.radio.lastChild.remove();
    setTimeout(() => d.classList.add('fade'), 6000);
  }

  hitmarker(head) {
    this.el.hitmarker.classList.add('on');
    this.el.hitmarker.classList.toggle('head', !!head);
    clearTimeout(this._hmT);
    this._hmT = setTimeout(() => this.el.hitmarker.classList.remove('on'), 120);
  }

  setScope(on) { this.el.scope.hidden = !on; this.el.crosshair.style.opacity = on ? '0' : '1'; }
  setFlash(v) { this.el.flash.style.opacity = String(Math.min(1, Math.max(0, v))); }
  setPlant(p, label) {
    if (p == null) { this.el.plantBar.hidden = true; return; }
    this.el.plantBar.hidden = false;
    this.el.plantLabel.textContent = label;
    this.el.plantFill.style.width = Math.round(p * 100) + '%';
  }
  showBuy(on, money) {
    this.el.buyMenu.hidden = !on;
    if (on) this.el.buyMoney.textContent = '$' + money;
  }
  setHint(text) { this.el.hint.textContent = text || ''; }

  // Per-frame cheap text updates (only when values change).
  update(state) {
    const p = state.player;
    const w = WEAPONS[p.weapon];
    this.el.health.textContent = p.health;
    this.el.healthBar.style.width = p.health + '%';
    this.el.healthBar.classList.toggle('low', p.health <= 30);
    this.el.armor.textContent = p.armor > 0 ? '◈ ' + p.armor : '';
    this.el.ammoMag.textContent = p.reloading ? '--' : p.mag;
    this.el.ammoReserve.textContent = '/ ' + p.reserve;
    this.el.ammoMag.parentElement.classList.toggle('empty', p.mag === 0);
    this.el.weaponName.textContent = w.name + (p.reloading ? ' — reloading' : '');
    const t = Math.max(0, Math.ceil(state.timeLeft));
    this.el.timer.textContent = Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0');
    this.el.timer.classList.toggle('urgent', t <= 10 && state.phase === 'round');
    this.el.phase.textContent = state.phaseLabel;
    this.el.score.textContent = `${state.score.jackets} — ${state.score.wardens}`;
    this.el.money.textContent = '$' + p.money;
    this.el.kills.textContent = `K ${p.kills} · D ${p.deaths}`;
    this.el.round.textContent = `ROUND ${state.round} / ${GAME.maxRounds}`;
    const g = [];
    if (p.grenades.smoke) g.push('◍ smoke ×' + p.grenades.smoke);
    if (p.grenades.flash) g.push('✦ flash ×' + p.grenades.flash);
    if (p.grenades.he) g.push('✸ surge ×' + p.grenades.he);
    this.el.grenades.textContent = g.join('  ') + (g.length ? '  ·  [4/5/6 throw]' : '');
    // Minimap at 8 Hz.
    const now = performance.now();
    if (now - this.lastMinimap >= 125) { this.lastMinimap = now; this.drawMinimap(state); }
  }

  drawMinimap(state) {
    const c = this.minimapCtx;
    const W = 168, H = 126;
    c.clearRect(0, 0, W, H);
    c.fillStyle = 'rgba(12,18,20,0.82)';
    c.fillRect(0, 0, W, H);
    const sx = (x) => (x + 32) / 64 * W;
    const sy = (z) => (z + 24) / 48 * H;
    // Walls.
    c.fillStyle = '#5a6a70';
    for (const b of state.walls) c.fillRect(sx(b.x0), sy(b.z0), Math.max(1, sx(b.x1) - sx(b.x0)), Math.max(1, sy(b.z1) - sy(b.z0)));
    // Sites.
    for (const key of ['A', 'B']) {
      const s = state.sites[key];
      c.fillStyle = key === 'A' ? 'rgba(255,179,71,0.25)' : 'rgba(47,214,168,0.25)';
      c.beginPath(); c.arc(sx(s.x), sy(s.z), 9, 0, 7); c.fill();
      c.fillStyle = key === 'A' ? '#ffb347' : '#2fd6a8';
      c.font = 'bold 11px system-ui'; c.textAlign = 'center';
      c.fillText(key, sx(s.x), sy(s.z) + 4);
    }
    // Planted core.
    if (state.plantState === 'planted' && state.plantPos) {
      c.fillStyle = '#ff5a3c';
      c.beginPath(); c.arc(sx(state.plantPos.x), sy(state.plantPos.z), 5, 0, 7); c.fill();
    }
    // Actors: teammates green, enemies red only when recently seen.
    const drawDot = (a, color) => {
      c.fillStyle = color;
      c.beginPath(); c.arc(sx(a.pos.x), sy(a.pos.z), a.isPlayer ? 4.5 : 3.5, 0, 7); c.fill();
    };
    for (const b of state.bots) {
      if (b.dead) continue;
      const ally = b.team === state.player.team;
      if (ally) drawDot(b, '#2fd6a8');
      else if (state.now - b.lastSeenAt < 3) drawDot(b, '#ff5a3c');
    }
    // Player arrow.
    const p = state.player;
    c.save();
    c.translate(sx(p.pos.x), sy(p.pos.z));
    c.rotate(Math.atan2(-Math.sin(p.yaw), -Math.cos(p.yaw)) + Math.PI / 2);
    c.fillStyle = '#ffffff';
    c.beginPath(); c.moveTo(0, -6); c.lineTo(4.5, 5); c.lineTo(-4.5, 5); c.closePath(); c.fill();
    c.restore();
    c.strokeStyle = 'rgba(255,255,255,0.35)';
    c.strokeRect(0.5, 0.5, W - 1, H - 1);
  }
}
