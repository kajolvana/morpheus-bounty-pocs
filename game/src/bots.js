// bots.js — nine bots, one brain each. A bot thinks 10 times a second:
// pick a route to its assigned site, hold it, push when the team does,
// engage what it can see (with a human reaction delay), plant the Tide
// Core when it carries it, defuse when the other team plants. Bots hear
// gunfire, throw smoke on executes and flash before entries, and talk on
// the radio so the player can follow the plan.
import * as THREE from 'three';
import { GAME, WEAPONS, CALLSIGNS, clamp, rng } from './config.js';
import { collideMove, hasLOS, nearestWaypoint, findRoute, SITES_POS, WAYPOINTS } from './world.js';
import { sfx } from './audio.js';

const THINK_HZ = 10;
const REACT_MIN = 0.45, REACT_MAX = 1.1;

function botMesh(teamColor) {
  const g = new THREE.Group();
  const suit = new THREE.MeshStandardMaterial({ color: 0x2e3438, roughness: 0.85, metalness: 0.2 });
  const vest = new THREE.MeshStandardMaterial({ color: teamColor, roughness: 0.7, metalness: 0.2 });
  const skin = new THREE.MeshStandardMaterial({ color: 0xc9a184, roughness: 0.9 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.52, 0.78, 0.3), suit);
  body.position.y = 1.05; g.add(body);
  const vestM = new THREE.Mesh(new THREE.BoxGeometry(0.56, 0.42, 0.34), vest);
  vestM.position.y = 1.18; g.add(vestM);
  const head = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.3, 0.3), skin);
  head.position.y = 1.62; g.add(head);
  const helm = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.14, 0.34), vest);
  helm.position.y = 1.78; g.add(helm);
  const legL = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.62, 0.22), suit);
  legL.position.set(-0.14, 0.31, 0); g.add(legL);
  const legR = legL.clone(); legR.position.x = 0.14; g.add(legR);
  const gun = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.12, 0.85), new THREE.MeshStandardMaterial({ color: 0x14181a, roughness: 0.5, metalness: 0.6 }));
  gun.position.set(0.22, 1.25, 0.3); g.add(gun);
  // Team ring under the feet: green for allies of the player, red for foes
  // is decided by hud/minimap; the mesh itself just carries the team color.
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.34, 0.44, 20),
    new THREE.MeshBasicMaterial({ color: teamColor, transparent: true, opacity: 0.7, side: THREE.DoubleSide }));
  ring.rotation.x = -Math.PI / 2; ring.position.y = 0.03; g.add(ring);
  g.userData = { body, head, legL, legR, gun };
  return g;
}

export class Bot {
  constructor(name, team, color) {
    this.name = name;
    this.team = team;
    this.color = color;
    this.pos = { x: 0, y: 0, z: 0 };
    this.yaw = 0; this.pitch = 0;
    this.health = 100; this.armor = 0;
    this.dead = false;
    this.weapon = 'tidepistol';
    this.mag = 12; this.reserve = 48;
    this.grenades = { smoke: 0, flash: 0, he: 0 };
    this.spread = 0.02;
    this.recoilPitch = 0; this.recoilYaw = 0;
    this.lastShotAt = 0; this.timeNow = 0;
    this.crouch = false;
    this.isCarrier = false;
    this.state = 'route';
    this.route = []; this.routeIdx = 0;
    this.site = 'A';
    this.holdPos = null;
    this.target = null;
    this.lastSeenPos = null;
    this.lastSeenAt = -99;
    this.reactUntil = 0;
    this.thinkAt = 0;
    this.planting = 0; this.defusing = 0;
    this.walkPhase = 0;
    this.speed = 3.6 + Math.random() * 0.9;
    this.aimErr = 0.03 + Math.random() * 0.02;
    this.flashUntil = 0; this.flashStrength = 0;
    this.kills = 0;
    this.threwSmoke = false; this.threwFlash = false;
    this.mesh = null;
    this.heardAt = null; this.heardPos = null;
  }

  spawn(spawn, team) {
    this.pos.x = spawn.x; this.pos.z = spawn.z; this.pos.y = 0;
    this.yaw = spawn.yaw; this.pitch = 0;
    this.health = 100; this.armor = 0; this.dead = false;
    this.mag = WEAPONS[this.weapon].mag;
    this.reserve = WEAPONS[this.weapon].reserve;
    this.state = 'route'; this.route = []; this.routeIdx = 0;
    this.target = null; this.lastSeenPos = null;
    this.planting = 0; this.defusing = 0;
    this.flashUntil = 0; this.flashStrength = 0;
    this.threwSmoke = false; this.threwFlash = false;
    this.heardAt = null;
    if (this.mesh) this.mesh.visible = true;
  }

  get eyeY() { return this.pos.y + 1.55; }
  get speed2d() { return this._sp || 0; }

  takeDamage(dmg) {
    if (this.dead) return false;
    let rest = dmg;
    if (this.armor > 0) {
      const absorbed = Math.min(this.armor, dmg * 0.5);
      this.armor = Math.round(this.armor - absorbed); rest = dmg - absorbed;
    }
    this.health = Math.max(0, Math.round(this.health - rest));
    if (this.health <= 0) { this.health = 0; this.dead = true; if (this.mesh) this.mesh.visible = false; return true; }
    return false;
  }

  // --- decisions (10 Hz) ------------------------------------------------
  think(now, ctx) {
    // ctx: {player, bots, phase, plantState, site, attackers, heard: [{pos, at}], radio(msg)}
    if (this.dead) return;
    const enemies = ctx.bots.filter(b => b.team !== this.team && !b.dead);
    if (!ctx.player.dead && ctx.player.team !== this.team) enemies.push(ctx.player);
    // Vision: nearest visible enemy (LOS + smoke + range + facing cone).
    let seen = null, seenD = 1e9;
    for (const e of enemies) {
      const dx = e.pos.x - this.pos.x, dz = e.pos.z - this.pos.z;
      const d = Math.hypot(dx, dz);
      if (d > 55) continue;
      const eyeA = { x: this.pos.x, y: this.eyeY, z: this.pos.z };
      const eyeB = { x: e.pos.x, y: (e.eyeY || 1.55), z: e.pos.z };
      if (!hasLOS(eyeA, eyeB)) continue;
      if (ctx.smokeBlocks && ctx.smokeBlocks(eyeA, eyeB)) continue;
      if (this.flashUntil > now) continue;
      const facing = (-Math.sin(this.yaw) * dx + -Math.cos(this.yaw) * dz) / Math.max(0.01, d);
      const heardRecently = this.heardAt && now - this.heardAt < 2.5;
      if (facing < 0.1 && d > 6 && !heardRecently) continue;
      if (d < seenD) { seenD = d; seen = e; }
    }
    if (seen) {
      if (this.target !== seen) this.reactUntil = now + REACT_MIN + Math.random() * (REACT_MAX - REACT_MIN);
      this.target = seen;
      this.lastSeenPos = { x: seen.pos.x, z: seen.pos.z };
      this.lastSeenAt = now;
    } else if (this.target && now - this.lastSeenAt > 1.6) {
      this.target = null;
    }
    // Hearing: a recent shot nearby pulls the bot's attention.
    if (ctx.heard.length && !this.target) {
      const h = ctx.heard[ctx.heard.length - 1];
      if (now - h.at < 1.2) {
        const d = Math.hypot(h.pos.x - this.pos.x, h.pos.z - this.pos.z);
        if (d < 26) { this.heardAt = now; this.heardPos = { ...h.pos }; }
      }
    }
    const attacking = ctx.attackers === this.team;
    const corePlanted = ctx.plantState === 'planted';
    // Role logic.
    if (corePlanted) {
      if (attacking) { this.state = 'hold'; this.holdPos = ctx.plantPos ? { x: ctx.plantPos.x + (Math.random() - 0.5) * 8, z: ctx.plantPos.z + (Math.random() - 0.5) * 8 } : this.holdPos; this.setRouteTo(this.holdPos); }
      else { this.state = 'defuse'; this.setRouteTo(ctx.plantPos); }
    } else if (attacking) {
      if (ctx.phase === 'round' && this.isCarrier && this.atSite(ctx)) { this.state = 'plant'; }
      else if (this.state === 'plant' && !this.atSite(ctx)) { this.state = 'route'; }
      else if (this.state !== 'plant') {
        if (this.state === 'hold' && this.target) this.state = 'route';
        if (!this.route.length || this.routeIdx >= this.route.length) {
          const dest = this.holdPos || SITES_POS[this.site];
          this.setRouteTo(dest);
        }
      }
    } else {
      // Defenders: hold near their site, rotate on radio / sightings.
      if (this.target && this.lastSeenPos) {
        const d = Math.hypot(this.lastSeenPos.x - this.pos.x, this.lastSeenPos.z - this.pos.z);
        if (d > 4 && now - this.lastSeenAt < 1.2) { this.state = 'route'; this.setRouteTo(this.lastSeenPos); }
      } else if (!this.holdPos || this.state === 'route') {
        this.state = 'hold';
        this.setRouteTo(this.holdPos || SITES_POS[this.site]);
      }
    }
    // Execute grenades: smoke the site entrance once, close in.
    if (attacking && !this.threwSmoke && this.nearSite(ctx, 12)) {
      this.threwSmoke = true;
      if (this.grenades.smoke > 0) { ctx.throwGrenade(this, 'smoke'); ctx.radio(`${this.name}: smoke on ${this.site}, pushing in`); }
    }
    if (attacking && !this.threwFlash && this.nearSite(ctx, 7)) {
      this.threwFlash = true;
      if (this.grenades.flash > 0) { ctx.throwGrenade(this, 'flash'); ctx.radio(`${this.name}: flashing ${this.site}`); }
    }
    // Callouts on first sighting, at most one per bot per 6 seconds.
    if (seen && now - this.lastSeenAt < 0.15 && now - (this.lastRadioAt || 0) > 6 && Math.random() < 0.6) {
      this.lastRadioAt = now;
      ctx.radio(`${this.name}: contact ${this.site === 'A' ? 'Furnace' : 'Pumps'} side`);
    }
  }

  atSite(ctx) {
    const s = SITES_POS[this.site];
    return Math.hypot(this.pos.x - s.x, this.pos.z - s.z) < s.r;
  }
  nearSite(ctx, r) {
    const s = SITES_POS[this.site];
    return Math.hypot(this.pos.x - s.x, this.pos.z - s.z) < r;
  }

  setRouteTo(dest) {
    if (!dest) return;
    const from = nearestWaypoint(this.pos.x, this.pos.z);
    const to = nearestWaypoint(dest.x, dest.z);
    const ids = findRoute(from, to);
    this.route = ids.map(id => ({ x: WAYPOINTS[id].x, z: WAYPOINTS[id].z }));
    this.route.push({ x: dest.x, z: dest.z });
    this.routeIdx = 0;
  }

  // --- movement + combat (fixed step) ------------------------------------
  step(dt, now, ctx) {
    this.timeNow = now;
    if (this.dead) return;
    if (now >= this.thinkAt) { this.thinkAt = now + 1 / THINK_HZ; this.think(now, ctx); }
    let moveX = 0, moveZ = 0;
    let wantYaw = this.yaw;
    // Aim at the target when engaged.
    if (this.target && !this.target.dead && now >= this.reactUntil) {
      const dx = this.target.pos.x - this.pos.x, dz = this.target.pos.z - this.pos.z;
      wantYaw = Math.atan2(-dx, -dz);
      this.pitch = clamp(Math.atan2((this.target.eyeY || 1.5) - this.eyeY, Math.hypot(dx, dz)), -0.6, 0.6);
    } else if (this.heardPos && now - (this.heardAt || 0) < 2) {
      wantYaw = Math.atan2(-(this.heardPos.x - this.pos.x), -(this.heardPos.z - this.pos.z));
    } else if (this.route.length && this.routeIdx < this.route.length) {
      const wp = this.route[this.routeIdx];
      const dx = wp.x - this.pos.x, dz = wp.z - this.pos.z;
      const d = Math.hypot(dx, dz);
      if (d < 0.9) { this.routeIdx++; }
      else {
        wantYaw = Math.atan2(-dx, -dz);
        const sp = this.target ? this.speed * 0.35 : this.speed;
        moveX = -Math.sin(wantYaw) * sp; moveZ = -Math.cos(wantYaw) * sp;
      }
    } else if (this.state === 'hold' && this.holdPos) {
      const dx = this.holdPos.x - this.pos.x, dz = this.holdPos.z - this.pos.z;
      if (Math.hypot(dx, dz) > 1.2) {
        wantYaw = Math.atan2(-dx, -dz);
        moveX = -Math.sin(wantYaw) * this.speed * 0.5;
        moveZ = -Math.cos(wantYaw) * this.speed * 0.5;
      } else {
        // Hold: sweep the yaw slowly to watch the lane.
        wantYaw = this.yaw + Math.sin(now * 0.5 + this.walkPhase) * 0.004;
      }
    }
    // Smooth turn.
    let dy = wantYaw - this.yaw;
    while (dy > Math.PI) dy -= 2 * Math.PI;
    while (dy < -Math.PI) dy += 2 * Math.PI;
    this.yaw += clamp(dy, -2.6 * dt, 2.6 * dt);
    // Plant / defuse channels freeze the bot (live rounds only).
    if (ctx.phase === 'round' && this.state === 'plant' && this.isCarrier && this.atSite(ctx)) {
      moveX = 0; moveZ = 0;
      this.planting += dt;
      if (this.planting >= GAME.plantTime) { this.planting = 0; ctx.onPlanted(this); }
    } else this.planting = Math.max(0, this.planting - dt * 2);
    if (this.state === 'defuse' && ctx.phase === 'round' && ctx.plantState === 'planted' && ctx.plantPos) {
      const d = Math.hypot(this.pos.x - ctx.plantPos.x, this.pos.z - ctx.plantPos.z);
      if (d < 2.2) {
        moveX = 0; moveZ = 0;
        this.defusing += dt;
        if (this.defusing >= GAME.defuseTime) { this.defusing = 0; ctx.onDefused(this); }
      } else this.defusing = 0;
    } else this.defusing = 0;
    // Move + collide.
    const before = { x: this.pos.x, z: this.pos.z };
    collideMove(this.pos, GAME.playerRadius, moveX * dt, moveZ * dt);
    this._sp = Math.hypot(this.pos.x - before.x, this.pos.z - before.z) / Math.max(dt, 1e-6);
    // Footsteps for nearby bots.
    if (this._sp > 0.5) {
      this.walkPhase += this._sp * dt;
      if (Math.floor(this.walkPhase / 2.3) !== Math.floor((this.walkPhase - this._sp * dt) / 2.3)) {
        const d = Math.hypot(this.pos.x - ctx.player.pos.x, this.pos.z - ctx.player.pos.z);
        if (d < 22) sfx.step(d, true);
      }
    }
    // Recoil decay, spread follows movement — plus range error: long
    // duels take longer than point-blank ones.
    this.recoilPitch *= Math.pow(0.0001, dt);
    this.recoilYaw *= Math.pow(0.0001, dt);
    const w = WEAPONS[this.weapon];
    const tgtD = this.target ? Math.hypot(this.target.pos.x - this.pos.x, this.target.pos.z - this.pos.z) : 0;
    this.spread = clamp(w.spread + (this._sp / 5.4) * w.moveSpread + this.aimErr + tgtD * 0.0012, 0.001, 0.22);
    // Fire in bursts at a visible target after the reaction delay (rounds only).
    if (ctx.phase === 'round' && !this.peaceful && this.target && !this.target.dead && now >= this.reactUntil && this.flashUntil <= now) {
      const d = Math.hypot(this.target.pos.x - this.pos.x, this.target.pos.z - this.pos.z);
      if (d < w.range * 0.9 && this.mag > 0 && now - this.lastShotAt >= w.interval * (w.auto ? 1 : 1.6)) {
        // Burst discipline: hold fire in short windows so bots don't laser.
        const burstOn = (now * 1.4 + this.walkPhase) % 1 < 0.4;
        if (burstOn) {
          const hit = ctx.fireWeapon(this, now);
          if (hit) ctx.onBotHit(this, this.target);
        }
      } else if (this.mag <= 0) {
        this.mag = w.mag; this.reserve = Math.max(0, this.reserve - w.mag);
      }
    }
    this.syncMesh(dt);
  }

  syncMesh(dt) {
    if (!this.mesh) return;
    this.mesh.position.set(this.pos.x, this.pos.y, this.pos.z);
    this.mesh.rotation.y = this.yaw + Math.PI;
    const u = this.mesh.userData;
    const swing = this._sp > 0.5 ? Math.sin(this.walkPhase * 3.2) * 0.45 : 0;
    u.legL.rotation.x = swing; u.legR.rotation.x = -swing;
    u.body.rotation.x = this.crouch ? 0.25 : 0;
  }
}

export function makeBots(scene, playerTeam) {
  const bots = [];
  const teamOf = (team) => (team === 'jackets' ? 0x2fd6a8 : 0xff8a4d);
  for (const team of ['jackets', 'wardens']) {
    const names = CALLSIGNS[team];
    const count = team === playerTeam ? 4 : 5;
    for (let i = 0; i < count; i++) {
      const b = new Bot(names[i], team, teamOf(team));
      b.mesh = botMesh(teamOf(team));
      scene.add(b.mesh);
      bots.push(b);
    }
  }
  return bots;
}

// Assign each bot a site + weapon + grenades for a round (seeded variety).
export function assignRound(bots, round, attackers, seed) {
  const rand = rng(seed * 7919 + round * 131);
  const atk = bots.filter(b => b.team === attackers);
  const def = bots.filter(b => b.team !== attackers);
  // Attackers split 3/2 across sites; defenders hold 3 on A / 2 on B or so.
  atk.forEach((b, i) => {
    b.site = i < 3 ? 'A' : 'B';
    b.isCarrier = i === 0;
    b.holdPos = null;
    const roll = rand();
    b.weapon = roll < 0.45 ? 'forge' : roll < 0.7 ? 'gale' : roll < 0.85 ? 'breaker' : 'longtide';
    b.grenades = { smoke: rand() < 0.6 ? 1 : 0, flash: rand() < 0.6 ? 1 : 0, he: rand() < 0.3 ? 1 : 0 };
    b.mag = WEAPONS[b.weapon].mag; b.reserve = WEAPONS[b.weapon].reserve;
    b.armor = rand() < 0.5 ? 100 : 0;
  });
  def.forEach((b, i) => {
    b.site = i < 3 ? 'A' : 'B';
    b.isCarrier = false;
    const s = SITES_POS[b.site];
    b.holdPos = { x: s.x + (rand() - 0.5) * 7, z: s.z + (rand() - 0.5) * 7 };
    const roll = rand();
    b.weapon = roll < 0.4 ? 'forge' : roll < 0.65 ? 'gale' : roll < 0.8 ? 'longtide' : 'breaker';
    b.grenades = { smoke: 0, flash: rand() < 0.3 ? 1 : 0, he: rand() < 0.2 ? 1 : 0 };
    b.mag = WEAPONS[b.weapon].mag; b.reserve = WEAPONS[b.weapon].reserve;
    b.armor = rand() < 0.5 ? 100 : 0;
  });
}
