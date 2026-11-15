// grenades.js — smoke, flash and HE. Thrown on an arc with gravity, they
// bounce once off the floor, then do their job. Smoke is a cluster of
// billboard puffs that also blocks bot line-of-sight; flash whites out
// anyone facing it; HE hurts in a radius. One pool, no per-frame allocation.
import * as THREE from 'three';
import { clamp } from './config.js';
import { collideMove, hasLOS } from './world.js';
import { sfx } from './audio.js';

const _v = new THREE.Vector3();

// A smoke puff: sprite that grows and drifts.
function makePuffTexture() {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 128;
  const g = cv.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 4, 64, 64, 64);
  grad.addColorStop(0, 'rgba(200,205,200,0.85)');
  grad.addColorStop(0.6, 'rgba(170,175,170,0.5)');
  grad.addColorStop(1, 'rgba(160,165,160,0)');
  g.fillStyle = grad; g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(cv);
}

export class GrenadeSystem {
  constructor(scene) {
    this.scene = scene;
    this.live = [];      // flying grenades {kind, pos, vel, t, owner}
    this.smokes = [];    // active smoke clouds {pos, puffs[], t, life}
    this.puffTex = makePuffTexture();
    this.flashUntil = 0; // player flash end (performance.now seconds)
    this.flashStrength = 0;
    this.onExplode = null; // (pos, radius, damage, owner) => void
  }

  throwGrenade(kind, from, yaw, pitch, owner) {
    const speed = 13;
    const vel = {
      x: -Math.sin(yaw) * Math.cos(pitch) * speed,
      y: Math.sin(pitch) * speed + 2.5,
      z: -Math.cos(yaw) * Math.cos(pitch) * speed,
    };
    this.live.push({ kind, pos: { x: from.x, y: from.y, z: from.z }, vel, t: 0, owner, bounced: false });
    sfx.pin();
  }

  update(dt, now, actors) {
    // Flying grenades.
    for (let i = this.live.length - 1; i >= 0; i--) {
      const g = this.live[i];
      g.t += dt;
      g.vel.y -= 12 * dt;
      const p = { x: g.pos.x, y: g.pos.y, z: g.pos.z };
      collideMove(p, 0.12, g.vel.x * dt, g.vel.z * dt);
      g.pos.x = p.x; g.pos.z = p.z;
      g.pos.y += g.vel.y * dt;
      if (g.pos.y < 0.12) {
        g.pos.y = 0.12;
        if (!g.bounced) { g.bounced = true; g.vel.y *= -0.35; g.vel.x *= 0.6; g.vel.z *= 0.6; sfx.bounce(); }
        else { g.vel.y = 0; g.vel.x *= 0.8; g.vel.z *= 0.8; }
      }
      const fuse = g.kind === 'he' ? 1.6 : 1.1;
      if (g.t >= fuse) {
        this.live.splice(i, 1);
        this.detonate(g, now, actors);
      }
    }
    // Smoke clouds grow, drift, fade after 14s.
    for (let i = this.smokes.length - 1; i >= 0; i--) {
      const s = this.smokes[i];
      s.t += dt;
      for (const puff of s.puffs) {
        const k = Math.min(1, s.t / 1.2);
        puff.scale.setScalar(puff.userData.base * (0.6 + k * 0.9));
        puff.material.opacity = 0.5 * (s.t > s.life - 3 ? Math.max(0, (s.life - s.t) / 3) : 1);
        puff.position.x += Math.sin(s.t * 0.3 + puff.userData.seed) * dt * 0.15;
      }
      if (s.t >= s.life) {
        for (const puff of s.puffs) this.scene.remove(puff);
        this.smokes.splice(i, 1);
      }
    }
  }

  detonate(g, now, actors) {
    if (g.kind === 'smoke') {
      const puffs = [];
      for (let i = 0; i < 9; i++) {
        const m = new THREE.SpriteMaterial({ map: this.puffTex, transparent: true, opacity: 0.5, depthWrite: false });
        const sp = new THREE.Sprite(m);
        sp.position.set(
          g.pos.x + (Math.random() - 0.5) * 3.2,
          0.7 + Math.random() * 1.8,
          g.pos.z + (Math.random() - 0.5) * 3.2,
        );
        sp.userData = { base: 2.2 + Math.random() * 1.6, seed: Math.random() * 6 };
        sp.scale.setScalar(1);
        this.scene.add(sp);
        puffs.push(sp);
      }
      this.smokes.push({ pos: { ...g.pos }, puffs, t: 0, life: 14 });
    } else if (g.kind === 'flash') {
      sfx.flash();
      // Anyone within 26m with line of sight and facing the pop gets blinded.
      for (const a of actors) {
        if (a.dead) continue;
        const dx = g.pos.x - a.pos.x, dy = (g.pos.y + 0.5) - (a.pos.y + 1.5), dz = g.pos.z - a.pos.z;
        const dist = Math.hypot(dx, dy, dz);
        if (dist > 26) continue;
        if (!hasLOS({ x: a.pos.x, y: a.pos.y + 1.5, z: a.pos.z }, { x: g.pos.x, y: g.pos.y + 0.5, z: g.pos.z })) continue;
        // Facing: dot of look dir with direction to flash.
        const lookX = -Math.sin(a.yaw), lookZ = -Math.cos(a.yaw);
        const facing = (dx * lookX + dz * lookZ) / Math.max(0.01, dist);
        const strength = clamp(1 - dist / 26, 0.25, 1) * (facing > -0.2 ? 1 : 0.45);
        const dur = 0.6 + strength * 2.2;
        a.flashUntil = Math.max(a.flashUntil || 0, now + dur);
        a.flashStrength = Math.max(a.flashStrength || 0, strength);
        if (a.isPlayer) { this.flashUntil = a.flashUntil; this.flashStrength = a.flashStrength; }
      }
    } else if (g.kind === 'he') {
      const pl = actors.find(a => a.isPlayer);
      sfx.explosion(pl ? Math.hypot(g.pos.x - pl.pos.x, g.pos.z - pl.pos.z) : 0);
      if (this.onExplode) this.onExplode({ ...g.pos }, 6, 95, g.owner);
    }
  }

  // Is the segment between two eye points blocked by smoke? Sample the line.
  smokeBlocks(a, b) {
    for (const s of this.smokes) {
      for (let t = 0.2; t < 1; t += 0.2) {
        const x = a.x + (b.x - a.x) * t, y = a.y + (b.y - a.y) * t, z = a.z + (b.z - a.z) * t;
        const d = Math.hypot(x - s.pos.x, z - s.pos.z);
        if (d < 2.6 && y < 3.2) return true;
      }
    }
    return false;
  }

  playerFlash(now) {
    if (now < this.flashUntil) return this.flashStrength * clamp((this.flashUntil - now) / 1.5, 0, 1);
    return 0;
  }
}
