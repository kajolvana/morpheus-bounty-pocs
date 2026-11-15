// game.js — the match: one state object, phases, economy, plant/defuse,
// win conditions and the radio. The simulation steps at a fixed 60 Hz;
// rendering only reads this state, never writes it.
import { GAME, WEAPONS, TEAMS, SITES } from './config.js';
import { assignRound, makeBots } from './bots.js';
import { SPAWNS, SITES_POS } from './world.js';
import { sfx } from './audio.js';

export class Match {
  constructor(scene, player, weapons, grenades, hud) {
    this.scene = scene;
    this.player = player;
    this.weapons = weapons;
    this.grenades = grenades;
    this.hud = hud;
    this.bots = makeBots(scene, player.team);
    this.phase = 'buy';       // buy | round | over
    this.phaseLabel = 'BUY PHASE';
    this.round = 1;
    this.score = { jackets: 0, wardens: 0 };
    this.timeLeft = GAME.buyTime;
    this.timeNow = 0;
    this.attackers = 'jackets';
    this.plantState = 'none'; // none | planted
    this.plantPos = null;
    this.plantProgress = 0;   // player channel 0..1
    this.defuseProgress = 0;
    this.coreCarrier = null;
    this.heard = [];          // {pos, at} gunshots bots can hear
    this.lossStreak = { jackets: 0, wardens: 0 };
    this.roundOver = false;
    this.matchOver = false;
    this.winner = null;
    this.seed = 20261115;
    this.grenades.onExplode = (pos, radius, damage, owner) => this.explodeHE(pos, radius, damage, owner);
    this.startRound();
  }

  get playerAttacking() { return this.attackers === this.player.team; }

  startRound() {
    const half = this.round >= GAME.halftimeRound;
    this.attackers = half ? 'wardens' : 'jackets';
    this.player.team = half ? 'wardens' : 'jackets';
    assignRound(this.bots, this.round, this.attackers, this.seed);
    // Spawns.
    const pSpawn = SPAWNS[this.player.team][0];
    this.player.reset(pSpawn, this.player.team);
    const spawnIdx = { jackets: 0, wardens: 0 };
    for (const b of this.bots) {
      const list = SPAWNS[b.team];
      const s = list[1 + (spawnIdx[b.team]++ % (list.length - 1))];
      b.spawn(s, b.team);
    }
    this.coreCarrier = this.bots.find(b => b.team === this.attackers && b.isCarrier) || null;
    this.plantState = 'none';
    this.plantPos = null;
    this.plantProgress = 0;
    this.defuseProgress = 0;
    this.roundOver = false;
    this.phase = 'buy';
    this.phaseLabel = 'BUY PHASE';
    this.timeLeft = GAME.buyTime;
    this.heard = [];
    const atkName = TEAMS[this.attackers].name;
    this.hud.showBanner(`ROUND ${this.round}`, `${atkName} attack — plant the Tide Core at ${SITES.A.name} or ${SITES.B.name}`);
    this.hud.radioLine(`Plan: ${this.attackers === this.player.team ? 'we push' : 'they push'} ${this.coreCarrier ? 'with ' + this.coreCarrier.name + ' carrying the Core' : ''}`);
    this.hud.setHint('Buy with [B] — 1 rifle · 2 SMG · 3 shotgun · 4 sniper · 5 smoke · 6 flash · 7 surge · 8 armor');
    sfx.roundStart();
  }

  // --- combat ------------------------------------------------------------
  fireWeapon(shooter, now) {
    const targets = [...this.bots, this.player];
    const hit = this.weapons.fire(shooter, targets, (target, dmg, head, point) => {
      this.applyDamage(target, dmg, shooter, head);
    });
    const dist = shooter.isPlayer ? 0 : Math.hypot(shooter.pos.x - this.player.pos.x, shooter.pos.z - this.player.pos.z);
    sfx.shot(WEAPONS[shooter.weapon].sound, dist);
    if (shooter.isPlayer) {
      const tip = this.gunTip();
      this.weapons.muzzleAt(tip.x, tip.y, tip.z);
    }
    this.heard.push({ pos: { x: shooter.pos.x, z: shooter.pos.z }, at: now });
    if (this.heard.length > 12) this.heard.shift();
    return hit;
  }

  gunTip() {
    const p = this.player;
    const f = { x: -Math.sin(p.yaw), z: -Math.cos(p.yaw) };
    return { x: p.pos.x + f.x * 0.7, y: p.eyeY - 0.25, z: p.pos.z + f.z * 0.7 };
  }

  applyDamage(target, dmg, shooter, head) {
    if (target.dead || this.roundOver) return;
    // Bot-vs-bot fights run cooler so a round lasts longer than the walk in.
    if (!shooter.isPlayer && !target.isPlayer) dmg *= 0.4;
    const died = target.takeDamage(dmg);
    if (shooter.isPlayer && target !== this.player) {
      this.hud.hitmarker(head);
      if (head) sfx.headshot(); else sfx.hit();
    }
    if (died) {
      const sName = shooter.isPlayer ? 'You' : shooter.name;
      const tName = target.isPlayer ? 'You' : target.name;
      const wName = WEAPONS[shooter.weapon].short;
      this.hud.killfeedLine(`${sName} ✕ ${tName}  [${wName}${head ? ' · headshot' : ''}]`, shooter.team === this.player.team ? 'ally' : 'foe');
      shooter.kills = (shooter.kills || 0) + 1;
      if (shooter.isPlayer) { shooter.money = Math.min(GAME.maxMoney, shooter.money + GAME.killReward); sfx.kill(); }
      if (target.isPlayer) this.hud.showBanner('ELIMINATED', `by ${shooter.name || 'a Warden'} — spectate your team`, 2200);
      if (target === this.coreCarrier) {
        // Core drops: nearest living attacker picks it up.
        const next = this.bots.find(b => b.team === this.attackers && !b.dead && b !== target);
        if (next) { next.isCarrier = true; this.coreCarrier = next; this.hud.radioLine(`${next.name} picked up the Tide Core`); }
        else this.coreCarrier = null;
      }
      this.checkRoundEnd();
    }
  }

  explodeHE(pos, radius, damage, owner) {
    const all = [...this.bots, this.player];
    for (const a of all) {
      if (a.dead || a === owner) continue;
      const d = Math.hypot(a.pos.x - pos.x, (a.pos.y + 1.2) - pos.y, a.pos.z - pos.z);
      if (d < radius) this.applyDamage(a, damage * (1 - d / radius * 0.6), owner, false);
    }
  }

  // --- plant / defuse ------------------------------------------------------
  tryPlant(dt) {
    if (!this.playerAttacking || this.plantState !== 'none' || this.player.dead) return;
    const s = this.playerSite();
    if (!s) { this.plantProgress = 0; return; }
    this.plantProgress += dt / GAME.plantTime;
    if (Math.floor(this.plantProgress * 8) !== Math.floor((this.plantProgress - dt / GAME.plantTime) * 8)) sfx.plantBeep();
    if (this.plantProgress >= 1) {
      this.plantProgress = 0;
      this.plantState = 'planted';
      this.plantPos = { x: this.player.pos.x, z: this.player.pos.z };
      this.player.money = Math.min(GAME.maxMoney, this.player.money + GAME.plantReward);
      this.hud.radioLine('You: Tide Core planted — hold the site!');
      this.hud.showBanner('TIDE CORE PLANTED', `${SITES[s].name} — defend it until it blows`);
      sfx.plantBeep(true);
      this.timeLeft = GAME.coreTimer;
    }
  }

  tryDefuse(dt) {
    if (this.playerAttacking || this.plantState !== 'planted' || this.player.dead) return;
    const d = Math.hypot(this.player.pos.x - this.plantPos.x, this.player.pos.z - this.plantPos.z);
    if (d > 2.2) { this.defuseProgress = 0; return; }
    this.defuseProgress += dt / GAME.defuseTime;
    if (Math.floor(this.defuseProgress * 6) !== Math.floor((this.defuseProgress - dt / GAME.defuseTime) * 6)) sfx.defuseBeep();
    if (this.defuseProgress >= 1) {
      this.defuseProgress = 0;
      this.endRound(this.player.team, `Core defused by you`);
    }
  }

  playerSite() {
    for (const key of ['A', 'B']) {
      const s = SITES_POS[key];
      if (Math.hypot(this.player.pos.x - s.x, this.player.pos.z - s.z) < s.r) return key;
    }
    return null;
  }

  // --- round flow ----------------------------------------------------------
  checkRoundEnd() {
    if (this.roundOver) return;
    const atkAlive = this.aliveCount(this.attackers);
    const defAlive = this.aliveCount(this.attackers === 'jackets' ? 'wardens' : 'jackets');
    if (this.plantState === 'planted') return; // only timer/defuse ends it now
    if (atkAlive === 0) this.endRound(this.otherTeam(this.attackers), 'Attackers eliminated');
    else if (defAlive === 0) this.endRound(this.attackers, 'Defenders eliminated');
  }

  aliveCount(team) {
    let n = 0;
    if (!this.player.dead && this.player.team === team) n++;
    for (const b of this.bots) if (!b.dead && b.team === team) n++;
    return n;
  }

  otherTeam(t) { return t === 'jackets' ? 'wardens' : 'jackets'; }

  endRound(winner, reason) {
    if (this.roundOver) return;
    this.roundOver = true;
    this.phase = 'over';
    this.phaseLabel = 'ROUND OVER';
    this.score[winner] += 1;
    const playerWon = winner === this.player.team;
    const winPay = GAME.winReward, losePay = GAME.lossReward + this.lossStreak[winner === 'jackets' ? 'wardens' : 'jackets'] * GAME.lossStreakBonus;
    this.player.money = Math.min(GAME.maxMoney, this.player.money + (playerWon ? winPay : losePay));
    if (playerWon) { this.lossStreak[this.otherTeam(winner)] += 1; this.lossStreak[winner] = 0; }
    else { this.lossStreak[winner] = 0; this.lossStreak[this.otherTeam(winner)] += 1; }
    this.hud.showBanner(playerWon ? 'ROUND WON' : 'ROUND LOST', `${TEAMS[winner].name} take round ${this.round} — ${reason}`, 3400);
    if (playerWon) sfx.roundWin(); else sfx.roundLose();
    this.hud.killfeedLine(`Round ${this.round}: ${TEAMS[winner].name} — ${reason}`);
    this.timeLeft = 4;
    if (this.score[winner] >= GAME.winRounds || this.round >= GAME.maxRounds) {
      this.matchOver = true;
      this.winner = this.score.jackets > this.score.wardens ? 'jackets' : this.score.wardens > this.score.jackets ? 'wardens' : null;
      this.hud.showBanner(this.winner === this.player.team ? 'MATCH WON' : 'MATCH LOST',
        this.winner ? `${TEAMS[this.winner].name} win the match ${this.score.jackets} — ${this.score.wardens}` : `Draw ${this.score.jackets} — ${this.score.wardens}`, 8000);
    }
  }

  nextRound() {
    if (this.matchOver) { this.phase = 'over'; this.phaseLabel = 'MATCH OVER'; return; }
    this.round += 1;
    this.startRound();
  }

  // --- fixed step ------------------------------------------------------------
  step(dt, input) {
    this.timeNow += dt;
    const now = this.timeNow;
    if (this.phase === 'buy') {
      this.timeLeft -= dt;
      if (this.timeLeft <= 0) {
        this.phase = 'round';
        this.phaseLabel = 'ROUND LIVE';
        this.timeLeft = GAME.roundTime;
        this.hud.setHint('Plant the Tide Core [hold E on a site] · defuse [hold E near the Core]');
        this.hud.showBuy(false);
        sfx.roundStart();
      }
    } else if (this.phase === 'round') {
      this.timeLeft -= dt;
      if (this.plantState === 'planted') {
        if (this.timeLeft <= 0) this.endRound(this.attackers, 'Tide Core detonated');
      } else if (this.timeLeft <= 0) {
        this.endRound(this.otherTeam(this.attackers), 'Time expired — attackers failed to plant');
      }
    } else if (this.phase === 'over') {
      this.timeLeft -= dt;
      if (this.timeLeft <= 0) {
        if (this.matchOver) { this.phase = 'over'; this.timeLeft = 999; }
        else this.nextRound();
      }
    }
    // Player channels (hold E).
    if (input.plant && this.phase === 'round' && !this.player.dead) {
      if (this.playerAttacking) this.tryPlant(dt);
      else this.tryDefuse(dt);
    } else { this.plantProgress = 0; this.defuseProgress = 0; }
    // Bots.
    const ctx = {
      player: this.player,
      bots: this.bots,
      phase: this.phase,
      plantState: this.plantState,
      plantPos: this.plantPos,
      attackers: this.attackers,
      heard: this.heard,
      smokeBlocks: (a, b) => this.grenades.smokeBlocks(a, b),
      radio: (m) => { this.hud.radioLine(m); sfx.radio(); },
      throwGrenade: (bot, kind) => {
        if (bot.grenades[kind] <= 0) return;
        bot.grenades[kind] -= 1;
        this.grenades.throwGrenade(kind, { x: bot.pos.x, y: bot.eyeY, z: bot.pos.z }, bot.yaw, 0.25, bot);
      },
      fireWeapon: (bot, t) => this.fireWeapon(bot, t),
      onBotHit: () => {},
      onPlanted: (bot) => {
        this.plantState = 'planted';
        this.plantPos = { x: bot.pos.x, z: bot.pos.z };
        this.timeLeft = GAME.coreTimer;
        this.hud.radioLine(`${bot.name}: Tide Core planted at ${SITES[bot.site].name}!`);
        this.hud.showBanner('TIDE CORE PLANTED', `${SITES[bot.site].name} — retake it or lose the round`);
        sfx.plantBeep(true);
      },
      onDefused: (bot) => this.endRound(bot.team, `Core defused by ${bot.name}`),
    };
    for (const b of this.bots) {
      if (this.phase === 'round' || this.phase === 'buy') b.step(dt, now, ctx);
    }
    this.grenades.update(dt, now, [...this.bots, this.player]);
  }

  hudState() {
    return {
      player: this.player,
      bots: this.bots,
      score: this.score,
      round: this.round,
      phase: this.phase,
      phaseLabel: this.phaseLabel,
      timeLeft: this.timeLeft,
      now: this.timeNow,
      sites: SITES_POS,
      walls: this.wallList,
      plantState: this.plantState,
      plantPos: this.plantPos,
    };
  }
}
