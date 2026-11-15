// TIDEFORGE config: one place for every number the game balances on.
// Teams: HARBORJACKETS (attackers, plant the Tide Core) vs FOUNDRY WARDENS
// (defenders, hold the sites). Sides swap at halftime (round 7).
export const GAME = {
  title: 'TIDEFORGE',
  mapName: 'Tideforge Foundry',
  roundTime: 100,
  buyTime: 15,
  plantTime: 4,
  defuseTime: 5,
  coreTimer: 40,
  winRounds: 7,
  maxRounds: 13,
  halftimeRound: 7,
  startMoney: 800,
  killReward: 300,
  winReward: 3250,
  lossReward: 1900,
  lossStreakBonus: 500,
  plantReward: 300,
  defuseReward: 300,
  maxMoney: 16000,
  playerSpeed: 5.4,
  crouchSpeed: 2.6,
  accel: 42,
  friction: 9,
  jumpSpeed: 5.2,
  gravity: 14.5,
  eyeHeight: 1.62,
  crouchEye: 1.1,
  playerRadius: 0.36,
  tickHz: 60,
};

// Weapons: name, price, damage (body/head), fire interval, mag, reserve,
// spread (rad, standing still), moveSpread, recoil, range, auto?, ads zoom.
export const WEAPONS = {
  tidepistol: { name: 'Tide Pistol', short: 'TP-9', price: 0, damage: 34, headMul: 3.0, interval: 0.16, mag: 12, reserve: 48, spread: 0.011, moveSpread: 0.05, recoil: 0.011, range: 60, auto: false, zoom: 1.15, tracer: 0xffd27a, sound: 'pistol' },
  gale: { name: 'Gale SMG', short: 'GALE-9', price: 1500, damage: 26, headMul: 3.2, interval: 0.075, mag: 32, reserve: 96, spread: 0.016, moveSpread: 0.055, recoil: 0.008, range: 45, auto: true, zoom: 1.25, tracer: 0xffe9a8, sound: 'smg' },
  forge: { name: 'Forge Rifle', short: 'FORGE-7', price: 2700, damage: 33, headMul: 4.0, interval: 0.1, mag: 30, reserve: 90, spread: 0.007, moveSpread: 0.045, recoil: 0.013, range: 90, auto: true, zoom: 1.35, tracer: 0xffc46b, sound: 'rifle' },
  breaker: { name: 'Breaker Shotgun', short: 'BRK-12', price: 1800, damage: 13, headMul: 2.0, interval: 0.85, mag: 6, reserve: 24, spread: 0.055, moveSpread: 0.09, recoil: 0.05, range: 22, auto: false, pellets: 8, zoom: 1.1, tracer: 0xff9d5c, sound: 'shotgun' },
  longtide: { name: 'Longtide Sniper', short: 'LNG-50', price: 4750, damage: 110, headMul: 2.2, interval: 1.45, mag: 5, reserve: 15, spread: 0.001, moveSpread: 0.16, recoil: 0.09, range: 200, auto: false, zoom: 4.0, scoped: true, tracer: 0x9fd8ff, sound: 'sniper' },
};

export const GRENADES = {
  smoke: { name: 'Tide Smoke', price: 300, key: 'smoke' },
  flash: { name: 'Flare Flash', price: 200, key: 'flash' },
  he: { name: 'Surge Charge', price: 300, key: 'he' },
};

export const ARMOR_PRICE = 650;
export const ARMOR_ABSORB = 0.5; // half of damage goes to armor first

export const TEAMS = {
  jackets: { name: 'Harborjackets', short: 'JACKETS', color: 0x2fae8f, css: '#2fd6a8', spawn: 'jackets' },
  wardens: { name: 'Foundry Wardens', short: 'WARDENS', color: 0xe07840, css: '#ff8a4d', spawn: 'wardens' },
};

// Bot callsigns, per team. Radio lines are built from these.
export const CALLSIGNS = {
  jackets: ['Rook', 'Vex', 'Marlin', 'Tess'],
  wardens: ['Ash', 'Brine', 'Cinder', 'Ferro', 'Lux'],
};

export const SITES = {
  A: { name: 'Furnace Hall', short: 'A' },
  B: { name: 'Pump Gallery', short: 'B' },
};

// Deterministic RNG (mulberry32) so ?shot= views are reproducible.
export function rng(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
export function lerp(a, b, t) { return a + (b - a) * t; }
