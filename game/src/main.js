// main.js — bootstrap: renderer, input, the fixed-step loop, the viewmodel
// gun and the ?shot= hooks that stage deterministic views for screenshots.
// The simulation (game.js) runs at a fixed 60 Hz accumulator; rendering
// only reads state. Pointer lock is only needed for live play — ?shot=
// views auto-start so headless screenshots work.
import * as THREE from 'three';
import { GAME, WEAPONS, rng } from './config.js';
import { buildWorld } from './world.js';
import { Player } from './player.js';
import { WeaponSystem } from './weapons.js';
import { GrenadeSystem } from './grenades.js';
import { Match } from './game.js';
import { HUD } from './hud.js';
import { ensureAudio, sfx } from './audio.js';

const params = new URLSearchParams(location.search);
const SHOT = params.get('shot') || '';
const SEED = parseInt(params.get('seed') || '1', 10);

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
document.getElementById('game').appendChild(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(74, innerWidth / innerHeight, 0.05, 220);
camera.rotation.order = 'YXZ';

const world = buildWorld(scene);
const player = new Player();
const weapons = new WeaponSystem(scene);
const grenades = new GrenadeSystem(scene);
const hud = new HUD(document);
const match = new Match(scene, player, weapons, grenades, hud);
match.wallList = world.colliders;

// --- viewmodel gun: a low-poly rifle built from boxes, bobbing with steps.
const gun = new THREE.Group();
{
  const dark = new THREE.MeshStandardMaterial({ color: 0x2a3134, roughness: 0.6, metalness: 0.4 });
  const accent = new THREE.MeshStandardMaterial({ color: 0x1f7a6d, roughness: 0.5, metalness: 0.4 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.13, 0.55), dark);
  const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.022, 0.022, 0.34, 8), dark);
  barrel.rotation.x = Math.PI / 2; barrel.position.set(0, 0.02, -0.42);
  const mag = new THREE.Mesh(new THREE.BoxGeometry(0.07, 0.2, 0.12), dark);
  mag.position.set(0, -0.14, -0.05); mag.rotation.x = 0.25;
  const sight = new THREE.Mesh(new THREE.BoxGeometry(0.025, 0.045, 0.07), accent);
  sight.position.set(0, 0.085, -0.08);
  const stock = new THREE.Mesh(new THREE.BoxGeometry(0.08, 0.11, 0.22), dark);
  stock.position.set(0, 0, 0.36);
  gun.add(body, barrel, mag, sight, stock);
  gun.position.set(0.26, -0.24, -0.45);
  gun.rotation.y = 0.1;
  camera.add(gun);
}
scene.add(camera);

// --- input ---------------------------------------------------------------
const input = { f: false, b: false, l: false, r: false, crouch: false, jump: false, walk: false, plant: false, fire: false };
let locked = false;
let started = false;

const overlay = document.getElementById('overlay');
const playBtn = document.getElementById('play');
function startGame() {
  ensureAudio();
  started = true;
  overlay.hidden = true;
  if (!SHOT) renderer.domElement.requestPointerLock?.();
}
playBtn.addEventListener('click', startGame);
document.addEventListener('pointerlockchange', () => {
  locked = document.pointerLockElement === renderer.domElement;
});
document.addEventListener('mousemove', (e) => {
  if (!locked || !started) return;
  player.look(e.movementX, e.movementY);
});
document.addEventListener('mousedown', (e) => {
  if (!started || !locked) return;
  if (e.button === 0) input.fire = true;
  if (e.button === 2) setAim(true);
});
document.addEventListener('mouseup', (e) => {
  if (e.button === 0) input.fire = false;
  if (e.button === 2) setAim(false);
});
document.addEventListener('contextmenu', (e) => e.preventDefault());

function setAim(on) {
  if (player.dead || player.reloading) on = false;
  player.aiming = on;
  const scoped = on && player.weapon === 'longtide';
  if (scoped !== player.scoped) { player.scoped = scoped; hud.setScope(scoped); if (scoped) sfx.scope(); }
}

const KEYMAP = { KeyW: 'f', KeyS: 'b', KeyA: 'l', KeyD: 'r', ControlLeft: 'crouch', ShiftLeft: 'walk', Space: 'jump', KeyE: 'plant' };
document.addEventListener('keydown', (e) => {
  if (e.code === 'KeyB' && match.phase === 'buy' && started) { hud.showBuy(hud.el.buyMenu.hidden, player.money); }
  if (e.code === 'KeyR') player.startReload(match.timeNow);
  if (e.code === 'Digit4') throwGrenade('smoke');
  if (e.code === 'Digit5') throwGrenade('flash');
  if (e.code === 'Digit6') throwGrenade('he');
  if (e.code === 'Digit1') buyWeapon('forge');
  if (e.code === 'Digit2') buyWeapon('gale');
  if (e.code === 'Digit3') buyWeapon('breaker');
  if (e.code === 'Digit7') buyWeapon('longtide');
  if (e.code === 'Digit8') { if (match.phase === 'buy') player.buyArmor(); }
  if (e.code === 'Digit9') { if (match.phase === 'buy') player.buyGrenade('smoke'); }
  if (e.code === 'Digit0') { if (match.phase === 'buy') player.buyGrenade('flash'); }
  if (KEYMAP[e.code]) input[KEYMAP[e.code]] = true;
  if (e.code === 'Space') e.preventDefault();
});
document.addEventListener('keyup', (e) => { if (KEYMAP[e.code]) input[KEYMAP[e.code]] = false; });

function buyWeapon(id) {
  if (match.phase !== 'buy' || !started) return;
  player.buyWeapon(id, match.timeNow);
}
function throwGrenade(kind) {
  if (!started || player.dead || player.grenades[kind] <= 0) return;
  player.grenades[kind] -= 1;
  grenades.throwGrenade(kind, { x: player.pos.x, y: player.eyeY, z: player.pos.z }, player.yaw, player.pitch + 0.25, player);
}

// Buy menu buttons.
document.querySelectorAll('#buy-menu [data-buy]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const kind = btn.dataset.buy;
    if (kind.startsWith('w:')) buyWeapon(kind.slice(2));
    else if (kind.startsWith('g:')) { if (match.phase === 'buy') player.buyGrenade(kind.slice(2)); }
    else if (kind === 'armor') { if (match.phase === 'buy') player.buyArmor(); }
    hud.showBuy(true, player.money);
  });
});
document.getElementById('buy-close').addEventListener('click', () => hud.showBuy(false));

// --- ?shot= staging --------------------------------------------------------
// Deterministic staged views for real screenshots of this code.
function stageShot() {
  const rand = rng(SEED * 101 + 7);
  started = true;
  overlay.hidden = true;
  // No ensureAudio() here: headless screenshots have no user gesture and
  // a suspended AudioContext only spams warnings; real play calls it on click.
  if (SHOT === 'start') {
    // Spawn view, buy phase open with the buy menu up.
    match.phase = 'buy'; match.timeLeft = 12;
    hud.showBuy(true, player.money);
    player.yaw = -Math.PI / 2 + 0.35;
  } else if (SHOT === 'buy') {
    match.phase = 'buy'; match.timeLeft = 9;
    player.money = 5150;
    player.buyWeapon('forge', 0);
    hud.showBuy(true, player.money);
    player.yaw = -Math.PI / 2 - 0.2;
  } else if (SHOT === 'play') {
    // Mid-round: round live, bots pushed up, one smoke cloud at mid.
    match.phase = 'round'; match.phaseLabel = 'ROUND LIVE'; match.timeLeft = 62;
    player.money = 5000;
    player.buyWeapon('forge', 0);
    player.pos.x = -12; player.pos.z = -4; player.yaw = -0.8;
    const jSpots = [[-4, -4], [-2, 2], [-5, 4], [-3, -6]];
    const wSpots = [[14, -8], [16, 6], [13, 0], [17, -4], [15, 8]];
    let ji = 0, wi = 0;
    for (const b of match.bots) {
      if (b.team === 'jackets') { const s = jSpots[ji++ % jSpots.length]; b.pos.x = s[0]; b.pos.z = s[1]; b.yaw = -Math.PI / 2; }
      else { const s = wSpots[wi++ % wSpots.length]; b.pos.x = s[0]; b.pos.z = s[1]; b.yaw = Math.PI / 2; }
    }
    grenades.throwGrenade('smoke', { x: 2, y: 1.5, z: -4 }, Math.PI / 2, 0.2, player);
  } else if (SHOT === 'plant') {
    // Planting the Core at Site A, plant bar mid-channel, defenders close.
    match.phase = 'round'; match.phaseLabel = 'ROUND LIVE'; match.timeLeft = 34;
    player.money = 5000;
    player.buyWeapon('forge', 0);
    player.pos.x = 18; player.pos.z = -11; player.yaw = -1.5;
    match.plantProgress = 0.15;
    input.plant = true;
    const jSpots = [[14, -8], [12, -14], [15, -6], [11, -10]];
    const wSpots = [[26, -4], [27, 0], [25, 2], [28, -8], [26, 4]];
    let ji = 0, wi = 0;
    for (const b of match.bots) {
      if (b.team === 'jackets') { const s = jSpots[ji++ % jSpots.length]; b.pos.x = s[0]; b.pos.z = s[1]; b.yaw = 0; }
      // Wardens hold their staged spot, facing east away from the site, and
      // hold fire (peaceful) so the planting player survives the 30-frame
      // screenshot window; the plant bar is the subject of this shot.
      else { const s = wSpots[wi++ % wSpots.length]; b.pos.x = s[0]; b.pos.z = s[1]; b.yaw = -Math.PI / 2; b.holdPos = { x: s[0], z: s[1] }; b.state = 'hold'; b.route = []; b.peaceful = true; }
    }
    hud.radioLine('Rook: Core going down at Furnace Hall');
    hud.killfeedLine('Vex ✕ Ash  [FORGE-7 · headshot]', 'ally');
  }
}

// --- main loop: fixed-step sim, render reads state -------------------------
let last = performance.now();
let acc = 0;
const STEP = 1 / GAME.tickHz;
let frames = 0;

function frame(now) {
  requestAnimationFrame(frame);
  let dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  if (started && !player.dead) {
    // Firing (hold for autos, click for semi — fire on edge for semi).
    if (input.fire && player.canFire(match.timeNow)) match.fireWeapon(player, match.timeNow);
    player.step(STEP, input, match.timeNow);
  }
  acc += dt;
  let n = 0;
  if (started) {
    while (acc >= STEP && n < 5) { match.step(STEP, input); acc -= STEP; n++; }
    if (n === 5) acc = 0;
  } else acc = 0;
  // Camera follows the player; recoil offsets the pitch.
  camera.position.set(player.pos.x, player.eyeY, player.pos.z);
  camera.rotation.y = player.yaw;
  camera.rotation.x = player.pitch + player.recoilPitch;
  camera.rotation.z = player.recoilYaw * 0.4;
  const targetFov = player.scoped ? 74 / 4 : player.aiming ? 58 : 74;
  camera.fov += (targetFov - camera.fov) * 0.25;
  camera.updateProjectionMatrix();
  // Gun bob + recoil kick.
  const bob = player.speed2d > 0.5 ? Math.sin(performance.now() / 140) * 0.012 * (player.speed2d / 5) : 0;
  gun.position.set(0.26 + (player.aiming ? -0.13 : 0), -0.24 + bob - (player.aiming ? 0.02 : 0), -0.45 + player.recoilPitch * 0.4);
  gun.visible = !player.dead;
  // HUD: plant/defuse progress + flash + minimap (8 Hz inside hud.update).
  if (match.phase === 'round') {
    if (match.plantProgress > 0) hud.setPlant(match.plantProgress, 'PLANTING TIDE CORE…');
    else if (match.defuseProgress > 0) hud.setPlant(match.defuseProgress, 'DEFUSING…');
    else hud.setPlant(null);
  } else hud.setPlant(null);
  hud.setFlash(grenades.playerFlash(match.timeNow));
  hud.update(match.hudState());
  weapons.update(dt);
  renderer.render(scene, camera);
  frames++;
  if (SHOT && frames === 30) window.__shotReady = true;
}

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

if (SHOT) stageShot();
// Debug handle for play-testing (read-only state inspection).
window.__tideforge = { match, player, grenades };
requestAnimationFrame(frame);
