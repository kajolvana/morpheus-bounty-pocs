// weapons.js — hitscan gunplay: spread grows with movement and falls when
// still/aiming, recoil kicks the view, headshots pay more. Tracers are a
// small pool of reused THREE.Line objects (no per-shot allocation).
import * as THREE from 'three';
import { WEAPONS, clamp } from './config.js';
import { rayWorld } from './world.js';
import { sfx } from './audio.js';

const _o = new THREE.Vector3();
const _d = new THREE.Vector3();
const _hit = new THREE.Vector3();
const TRACER_LIFE = 0.09;

export class WeaponSystem {
  constructor(scene) {
    this.scene = scene;
    this.tracers = [];
    for (let i = 0; i < 24; i++) {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
      const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0xffc46b, transparent: true, opacity: 0 }));
      line.frustumCulled = false;
      scene.add(line);
      this.tracers.push({ line, life: 0 });
    }
    this.tracerIdx = 0;
    // Muzzle flash: one reusable sprite at the gun tip.
    const cv = document.createElement('canvas');
    cv.width = cv.height = 64;
    const g = cv.getContext('2d');
    const grad = g.createRadialGradient(32, 32, 2, 32, 32, 32);
    grad.addColorStop(0, 'rgba(255,240,180,1)');
    grad.addColorStop(0.4, 'rgba(255,170,60,0.85)');
    grad.addColorStop(1, 'rgba(255,120,20,0)');
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    const tex = new THREE.CanvasTexture(cv);
    this.flash = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0, depthWrite: false }));
    this.flash.scale.set(0.55, 0.55, 1);
    this.flashLife = 0;
    scene.add(this.flash);
  }

  fire(shooter, targetList, onHit) {
    // shooter: {pos:Vector3-like {x,y,z}, yaw, pitch, weapon, mag, spreadState}
    const w = WEAPONS[shooter.weapon];
    if (shooter.mag <= 0) { sfx.dryFire(); return false; }
    shooter.mag -= 1;
    shooter.lastShotAt = shooter.timeNow;
    const pellets = w.pellets || 1;
    let anyHit = false;
    for (let p = 0; p < pellets; p++) {
      const spread = shooter.spread * (pellets > 1 ? 1 : 1);
      const yaw = shooter.yaw + (Math.random() - 0.5) * 2 * spread + shooter.recoilYaw;
      const pitch = shooter.pitch + (Math.random() - 0.5) * 2 * spread + shooter.recoilPitch;
      _d.set(-Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), -Math.cos(yaw) * Math.cos(pitch)).normalize();
      _o.set(shooter.pos.x, shooter.eyeY, shooter.pos.z);
      // Nearest world hit first.
      const wallT = rayWorld(_o, _d, w.range);
      let bestT = wallT < 0 ? w.range : wallT;
      let bestTarget = null, bestHead = false;
      for (const t of targetList) {
        if (t.dead || t === shooter) continue;
        // Body sphere (r 0.42 at chest 1.15) + head sphere (r 0.24 at 1.55).
        for (const part of [{ y: 1.15, r: 0.45, head: false }, { y: 1.55, r: 0.26, head: true }]) {
          const cx = t.pos.x - _o.x, cy = (t.pos.y + part.y - (t.crouch ? 0.45 : 0)) - _o.y, cz = t.pos.z - _o.z;
          const proj = cx * _d.x + cy * _d.y + cz * _d.z;
          if (proj < 0 || proj > bestT) continue;
          const px = _o.x + _d.x * proj, py = _o.y + _d.y * proj, pz = _o.z + _d.z * proj;
          const dd = (px - t.pos.x) ** 2 + (py - (t.pos.y + part.y - (t.crouch ? 0.45 : 0))) ** 2 + (pz - t.pos.z) ** 2;
          if (dd < part.r * part.r) { bestT = proj; bestTarget = t; bestHead = part.head; }
        }
      }
      _hit.copy(_o).addScaledVector(_d, bestT);
      this.spawnTracer(_o, _hit, w.tracer);
      if (bestTarget) {
        const dmg = w.damage * (bestHead ? w.headMul : 1);
        anyHit = true;
        onHit(bestTarget, dmg, bestHead, _hit);
      }
    }
    // Recoil: kicks pitch up, decays in the controller.
    shooter.recoilPitch += w.recoil * (0.7 + Math.random() * 0.6);
    shooter.recoilYaw += (Math.random() - 0.5) * w.recoil * 0.6;
    return anyHit;
  }

  spawnTracer(from, to, color) {
    const t = this.tracers[this.tracerIdx];
    this.tracerIdx = (this.tracerIdx + 1) % this.tracers.length;
    const pos = t.line.geometry.attributes.position;
    pos.setXYZ(0, from.x, from.y, from.z);
    pos.setXYZ(1, to.x, to.y, to.z);
    pos.needsUpdate = true;
    t.line.material.color.setHex(color);
    t.line.material.opacity = 0.9;
    t.life = TRACER_LIFE;
  }

  muzzleAt(x, y, z) {
    this.flash.position.set(x, y, z);
    this.flash.material.opacity = 1;
    this.flashLife = 0.05;
  }

  update(dt) {
    for (const t of this.tracers) {
      if (t.life > 0) {
        t.life -= dt;
        t.line.material.opacity = Math.max(0, t.life / TRACER_LIFE) * 0.9;
      }
    }
    if (this.flashLife > 0) {
      this.flashLife -= dt;
      this.flash.material.opacity = Math.max(0, this.flashLife / 0.05);
    }
  }
}

// Spread for a shooter: base + movement penalty, reduced by aiming/crouch.
export function computeSpread(w, speed, aiming, crouch) {
  let s = w.spread + (speed / 5.4) * w.moveSpread;
  if (aiming) s *= 0.45;
  if (crouch) s *= 0.8;
  return clamp(s, 0.0008, 0.22);
}
