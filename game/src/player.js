// player.js — the human: first-person controller with acceleration,
// friction, crouch, jump and AABB collision; recoil that decays back;
// footsteps wired to movement; weapon state (mag, reserve, reload).
import { GAME, WEAPONS } from './config.js';
import { computeSpread } from './weapons.js';
import { collideMove } from './world.js';
import { sfx } from './audio.js';

export class Player {
  constructor() {
    this.pos = { x: -28, y: 0, z: 0 };
    this.vel = { x: 0, z: 0 };
    this.vy = 0;
    this.yaw = Math.PI / 2;
    this.pitch = 0;
    this.crouch = false;
    this.onGround = true;
    this.isPlayer = true;
    this.dead = false;
    this.health = 100;
    this.armor = 0;
    this.money = GAME.startMoney;
    this.weapon = 'tidepistol';
    this.mag = WEAPONS.tidepistol.mag;
    this.reserve = WEAPONS.tidepistol.reserve;
    this.grenades = { smoke: 0, flash: 0, he: 0 };
    this.spread = 0.01;
    this.recoilPitch = 0;
    this.recoilYaw = 0;
    this.aiming = false;
    this.scoped = false;
    this.reloading = false;
    this.reloadUntil = 0;
    this.lastShotAt = 0;
    this.timeNow = 0;
    this.stepAcc = 0;
    this.kills = 0;
    this.deaths = 0;
    this.flashUntil = 0;
    this.flashStrength = 0;
    this.team = 'jackets';
  }

  get eyeY() { return this.pos.y + (this.crouch ? GAME.crouchEye : GAME.eyeHeight); }
  get speed2d() { return Math.hypot(this.vel.x, this.vel.z); }

  reset(spawn, team) {
    this.pos.x = spawn.x; this.pos.z = spawn.z; this.pos.y = 0;
    this.yaw = spawn.yaw; this.pitch = 0;
    this.vel.x = 0; this.vel.z = 0; this.vy = 0;
    this.dead = false; this.health = 100;
    this.recoilPitch = 0; this.recoilYaw = 0;
    this.reloading = false; this.aiming = false; this.scoped = false;
    this.team = team;
    this.flashUntil = 0; this.flashStrength = 0;
  }

  look(dx, dy) {
    const sens = this.scoped ? 0.0009 : 0.0021;
    this.yaw -= dx * sens;
    this.pitch -= dy * sens;
    const lim = Math.PI / 2 - 0.02;
    if (this.pitch > lim) this.pitch = lim;
    if (this.pitch < -lim) this.pitch = -lim;
  }

  // Fixed-step movement. input: {f,b,l,r,crouch,jump,walk}
  step(dt, input, now) {
    this.timeNow = now;
    if (this.dead) return;
    const w = WEAPONS[this.weapon];
    const wantCrouch = !!input.crouch;
    this.crouch = wantCrouch;
    const maxSpeed = (wantCrouch ? GAME.crouchSpeed : GAME.playerSpeed) * (input.walk ? 0.55 : 1) * (this.aiming ? 0.6 : 1);
    const sin = Math.sin(this.yaw), cos = Math.cos(this.yaw);
    let fx = 0, fz = 0;
    if (input.f) { fx += -sin; fz += -cos; }
    if (input.b) { fx += sin; fz += cos; }
    if (input.l) { fx += -cos; fz += sin; }
    if (input.r) { fx += cos; fz += -sin; }
    const fl = Math.hypot(fx, fz);
    if (fl > 0) { fx /= fl; fz /= fl; }
    this.vel.x += fx * GAME.accel * dt;
    this.vel.z += fz * GAME.accel * dt;
    // Friction when no input; clamp to max speed.
    const sp = this.speed2d;
    if (fl === 0) {
      const dec = Math.max(0, sp - GAME.friction * dt);
      if (sp > 0) { this.vel.x *= dec / sp; this.vel.z *= dec / sp; }
    } else if (sp > maxSpeed) {
      this.vel.x *= maxSpeed / sp; this.vel.z *= maxSpeed / sp;
    }
    collideMove(this.pos, GAME.playerRadius, this.vel.x * dt, this.vel.z * dt);
    // Jump + gravity.
    if (input.jump && this.onGround && !wantCrouch) { this.vy = GAME.jumpSpeed; this.onGround = false; }
    if (!this.onGround) {
      this.vy -= GAME.gravity * dt;
      this.pos.y += this.vy * dt;
      if (this.pos.y <= 0) { this.pos.y = 0; this.vy = 0; this.onGround = true; }
    }
    // Footsteps: distance-based, wired to real movement.
    const moved = this.speed2d * dt;
    if (this.onGround && moved > 0.001) {
      this.stepAcc += moved;
      const stride = wantCrouch ? 1.5 : 2.3;
      if (this.stepAcc >= stride) { this.stepAcc = 0; sfx.step(0, !input.walk); }
    }
    // Recoil decays; spread follows speed/aim/crouch.
    this.recoilPitch *= Math.pow(0.0001, dt);
    this.recoilYaw *= Math.pow(0.0001, dt);
    this.spread = computeSpread(w, this.speed2d, this.aiming, this.crouch);
    // Reload completes.
    if (this.reloading && now >= this.reloadUntil) {
      this.reloading = false;
      const need = w.mag - this.mag;
      const take = Math.min(need, this.reserve);
      this.mag += take; this.reserve -= take;
    }
  }

  startReload(now) {
    const w = WEAPONS[this.weapon];
    if (this.reloading || this.mag >= w.mag || this.reserve <= 0 || this.dead) return false;
    this.reloading = true;
    this.reloadUntil = now + (this.weapon === 'longtide' ? 2.6 : this.weapon === 'breaker' ? 2.4 : 1.7);
    this.scoped = false;
    sfx.reload();
    return true;
  }

  canFire(now) {
    const w = WEAPONS[this.weapon];
    return !this.dead && !this.reloading && this.mag > 0 && now - this.lastShotAt >= w.interval;
  }

  buyWeapon(id, now) {
    const w = WEAPONS[id];
    if (!w || this.money < w.price) return false;
    this.money -= w.price;
    this.weapon = id;
    this.mag = w.mag; this.reserve = w.reserve;
    this.reloading = false;
    sfx.buy();
    return true;
  }

  buyGrenade(kind) {
    const prices = { smoke: 300, flash: 200, he: 300 };
    if (this.money < prices[kind] || this.grenades[kind] >= 2) return false;
    this.money -= prices[kind];
    this.grenades[kind] += 1;
    sfx.buy();
    return true;
  }

  buyArmor() {
    if (this.money < 650 || this.armor >= 100) return false;
    this.money -= 650;
    this.armor = 100;
    sfx.buy();
    return true;
  }

  // Damage with armor absorbing half. Returns true if this killed the player.
  takeDamage(dmg) {
    if (this.dead) return false;
    let rest = dmg;
    if (this.armor > 0) {
      const absorbed = Math.min(this.armor, dmg * 0.5);
      this.armor = Math.round(this.armor - absorbed);
      rest = dmg - absorbed;
    }
    this.health = Math.max(0, Math.round(this.health - rest));
    sfx.hurt();
    if (this.health <= 0) { this.health = 0; this.dead = true; this.deaths += 1; return true; }
    return false;
  }
}
