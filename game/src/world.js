// world.js — the Tideforge Foundry map: geometry, collision, sightlines,
// nav graph and plant sites. One job: the place the game happens in.
// Layout (metres, x east, z south): Jackets spawn west, Wardens east.
// Site A Furnace Hall north-east, Site B Pump Gallery south-east, three
// lanes (North, Mid Concourse, South) between them.
import * as THREE from 'three';
import { SITES } from './config.js';

// Palette: deep teal steel, rust orange, warm sodium light, dark water.
export const PALETTE = {
  floor: 0x232b2e, floorLine: 0x2e3d40, wall: 0x3a4448, wallDark: 0x2b3336,
  rust: 0x8a4a28, rustLight: 0xb06a35, teal: 0x1f7a6d, tealGlow: 0x2fd6a8,
  amber: 0xffb347, furnace: 0xff7a2a, water: 0x14505a, pipe: 0x5a6a70,
  crate: 0x6b5a3e, crateDark: 0x4d4030,
};

export const MAP = { minX: -32, maxX: 32, minZ: -24, maxZ: 24 };

// AABB colliders: {x0,z0,x1,z1,h} — h is the top height (blocks sight above it).
const colliders = [];
function solid(x0, z0, x1, z1, h = 3.2) {
  colliders.push({ x0, z0, x1, z1, h });
  return colliders[colliders.length - 1];
}

// Waypoint graph for bots: id -> {x, z, links:[ids]}.
const WAYPOINTS = {
  spawnJ: { x: -28, z: 0, links: ['jMid', 'jNorth', 'jSouth'] },
  jMid: { x: -20, z: 0, links: ['spawnJ', 'midW', 'jNorth', 'jSouth'] },
  jNorth: { x: -20, z: -16, links: ['jMid', 'spawnJ', 'nLaneW', 'midW'] },
  jSouth: { x: -20, z: 16, links: ['jMid', 'spawnJ', 'sLaneW', 'midW'] },
  nLaneW: { x: -8, z: -18, links: ['jNorth', 'nLaneE', 'midW'] },
  nLaneE: { x: 8, z: -18, links: ['nLaneW', 'aWest', 'aSite', 'midE'] },
  sLaneW: { x: -8, z: 18, links: ['jSouth', 'sLaneE', 'midW'] },
  sLaneE: { x: 8, z: 18, links: ['sLaneW', 'bWest', 'bSite', 'midE'] },
  midW: { x: -8, z: 0, links: ['jMid', 'mid', 'nLaneW', 'sLaneW'] },
  mid: { x: 0, z: -5.5, links: ['midW', 'midE', 'nLaneW', 'sLaneW'] },
  midE: { x: 8, z: 0, links: ['mid', 'aWest', 'bWest', 'nLaneE', 'sLaneE'] },
  aWest: { x: 12, z: -10, links: ['midE', 'nLaneE', 'aSite'] },
  aSite: { x: 20, z: -14, links: ['aWest', 'nLaneE', 'aEast'] },
  aEast: { x: 27, z: -8, links: ['aSite', 'spawnW', 'bEast'] },
  bWest: { x: 12, z: 10, links: ['midE', 'sLaneE', 'bSite'] },
  bSite: { x: 20, z: 14, links: ['bWest', 'sLaneE', 'bEast'] },
  bEast: { x: 27, z: 12, links: ['bSite', 'aEast', 'spawnW'] },
  spawnW: { x: 28, z: 0, links: ['aEast', 'bEast', 'midE'] },
};

export const SITES_POS = {
  A: { x: 20, z: -14, r: 4.5 },
  B: { x: 20, z: 14, r: 4.5 },
};

export const SPAWNS = {
  jackets: [
    { x: -28, z: -4, yaw: -Math.PI / 2 }, { x: -28, z: 0, yaw: -Math.PI / 2 },
    { x: -28, z: 4, yaw: -Math.PI / 2 }, { x: -26, z: -2, yaw: -Math.PI / 2 },
    { x: -26, z: 2, yaw: -Math.PI / 2 },
  ],
  wardens: [
    { x: 28, z: -4, yaw: Math.PI / 2 }, { x: 28, z: 0, yaw: Math.PI / 2 },
    { x: 28, z: 4, yaw: Math.PI / 2 }, { x: 26, z: -2, yaw: Math.PI / 2 },
    { x: 26, z: 2, yaw: Math.PI / 2 },
  ],
};

// --- collision & sight -------------------------------------------------
// Move a circle (XZ) against all AABBs, axis by axis. Returns corrected pos.
export function collideMove(p, r, dx, dz) {
  let nx = p.x + dx;
  for (const c of colliders) {
    if (nx + r > c.x0 && nx - r < c.x1 && p.z + r > c.z0 && p.z - r < c.z1) {
      nx = dx > 0 ? c.x0 - r : c.x1 + r;
    }
  }
  let nz = p.z + dz;
  for (const c of colliders) {
    if (nx + r > c.x0 && nx - r < c.x1 && nz + r > c.z0 && nz - r < c.z1) {
      nz = dz > 0 ? c.z0 - r : c.z1 + r;
    }
  }
  p.x = Math.max(MAP.minX + r, Math.min(MAP.maxX - r, nx));
  p.z = Math.max(MAP.minZ + r, Math.min(MAP.maxZ - r, nz));
}

// Ray vs AABB slab test in 3D (origin o, dir d, max dist). Returns t or -1.
export function rayWorld(o, d, maxDist) {
  let best = maxDist;
  for (const c of colliders) {
    let t0 = 0, t1 = best;
    // X slab
    if (Math.abs(d.x) < 1e-9) { if (o.x < c.x0 || o.x > c.x1) continue; }
    else {
      let ta = (c.x0 - o.x) / d.x, tb = (c.x1 - o.x) / d.x;
      if (ta > tb) { const t = ta; ta = tb; tb = t; }
      t0 = Math.max(t0, ta); t1 = Math.min(t1, tb);
      if (t0 > t1) continue;
    }
    if (Math.abs(d.z) < 1e-9) { if (o.z < c.z0 || o.z > c.z1) continue; }
    else {
      let ta = (c.z0 - o.z) / d.z, tb = (c.z1 - o.z) / d.z;
      if (ta > tb) { const t = ta; ta = tb; tb = t; }
      t0 = Math.max(t0, ta); t1 = Math.min(t1, tb);
      if (t0 > t1) continue;
    }
    // Y slab: collider spans 0..h
    if (Math.abs(d.y) < 1e-9) { if (o.y < 0 || o.y > c.h) continue; }
    else {
      let ta = (0 - o.y) / d.y, tb = (c.h - o.y) / d.y;
      if (ta > tb) { const t = ta; ta = tb; tb = t; }
      t0 = Math.max(t0, ta); t1 = Math.min(t1, tb);
      if (t0 > t1) continue;
    }
    if (t0 < best && t0 >= 0) best = t0;
  }
  return best < maxDist ? best : -1;
}

// Line of sight between two eye positions (ignores low cover under 1.2m).
export function hasLOS(a, b) {
  const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
  const len = Math.hypot(dx, dy, dz);
  if (len < 0.01) return true;
  const t = rayWorld(a, { x: dx / len, y: dy / len, z: dz / len }, len);
  return t < 0;
}

export function nearestWaypoint(x, z) {
  let best = null, bd = 1e9;
  for (const id in WAYPOINTS) {
    const w = WAYPOINTS[id];
    const d = (w.x - x) ** 2 + (w.z - z) ** 2;
    if (d < bd) { bd = d; best = id; }
  }
  return best;
}

// Breadth-first route through the waypoint graph. Returns array of ids.
export function findRoute(fromId, toId) {
  if (fromId === toId) return [toId];
  const prev = { [fromId]: null };
  const q = [fromId];
  while (q.length) {
    const cur = q.shift();
    for (const n of WAYPOINTS[cur].links) {
      if (!(n in prev)) { prev[n] = cur; if (n === toId) { const path = [n]; let c = cur; while (c) { path.unshift(c); c = prev[c]; } return path; } q.push(n); }
    }
  }
  return [toId];
}

// --- visuals ------------------------------------------------------------
function canvasTexture(draw, w = 256, h = 256) {
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  draw(cv.getContext('2d'), w, h);
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function siteLabelTexture(letter, color) {
  return canvasTexture((g, w, h) => {
    g.fillStyle = 'rgba(10,14,16,0.85)';
    g.fillRect(0, 0, w, h);
    g.strokeStyle = color; g.lineWidth = 10; g.strokeRect(8, 8, w - 16, h - 16);
    g.fillStyle = color; g.font = 'bold 150px system-ui, sans-serif';
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText(letter, w / 2, h / 2 + 8);
  });
}

export function buildWorld(scene) {
  const group = new THREE.Group();
  scene.add(group);
  const steel = new THREE.MeshStandardMaterial({ color: PALETTE.wall, roughness: 0.85, metalness: 0.35 });
  const steelDark = new THREE.MeshStandardMaterial({ color: PALETTE.wallDark, roughness: 0.9, metalness: 0.3 });
  const rustMat = new THREE.MeshStandardMaterial({ color: PALETTE.rust, roughness: 0.95, metalness: 0.2 });
  const rustLight = new THREE.MeshStandardMaterial({ color: PALETTE.rustLight, roughness: 0.9, metalness: 0.25 });
  const tealMat = new THREE.MeshStandardMaterial({ color: PALETTE.teal, roughness: 0.7, metalness: 0.4 });
  const pipeMat = new THREE.MeshStandardMaterial({ color: PALETTE.pipe, roughness: 0.5, metalness: 0.7 });
  const crateMat = new THREE.MeshStandardMaterial({ color: PALETTE.crate, roughness: 0.95, metalness: 0.05 });
  const crateDark = new THREE.MeshStandardMaterial({ color: PALETTE.crateDark, roughness: 0.95, metalness: 0.05 });
  const floorMat = new THREE.MeshStandardMaterial({ color: PALETTE.floor, roughness: 0.92, metalness: 0.15 });
  const furnaceMat = new THREE.MeshStandardMaterial({ color: 0x3d2c22, roughness: 0.8, metalness: 0.3, emissive: PALETTE.furnace, emissiveIntensity: 0.28 });
  const waterMat = new THREE.MeshStandardMaterial({ color: PALETTE.water, roughness: 0.25, metalness: 0.1, emissive: 0x0a3a44, emissiveIntensity: 0.4 });
  const lampMat = new THREE.MeshStandardMaterial({ color: 0x332a18, emissive: PALETTE.amber, emissiveIntensity: 1.6 });

  function box(x0, z0, x1, z1, h, mat, y0 = 0) {
    const w = x1 - x0, d = z1 - z0;
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.position.set((x0 + x1) / 2, y0 + h / 2, (z0 + z1) / 2);
    m.castShadow = false; m.receiveShadow = false;
    group.add(m);
    solid(x0, z0, x1, z1, y0 + h);
    return m;
  }
  function decoBox(x, z, w, d, h, mat, y0 = 0) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.position.set(x, y0 + h / 2, z);
    group.add(m);
    return m;
  }
  function cyl(x, z, r, h, mat, y0 = 0, seg = 12) {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, seg), mat);
    m.position.set(x, y0 + h / 2, z);
    group.add(m);
    return m;
  }

  // Floor with painted lane lines (canvas texture, generated at runtime).
  const floorTex = canvasTexture((g, w, h) => {
    g.fillStyle = '#232b2e'; g.fillRect(0, 0, w, h);
    g.strokeStyle = 'rgba(47,214,168,0.25)'; g.lineWidth = 3;
    for (let i = 0; i < 16; i++) { // panel seams
      g.beginPath(); g.moveTo(i * 64, 0); g.lineTo(i * 64, h); g.stroke();
      g.beginPath(); g.moveTo(0, i * 64); g.lineTo(w, i * 64); g.stroke();
    }
    g.strokeStyle = 'rgba(255,179,71,0.55)'; g.lineWidth = 6; g.setLineDash([26, 18]);
    g.beginPath(); g.moveTo(60, h * 0.22); g.lineTo(w - 60, h * 0.22); g.stroke();
    g.beginPath(); g.moveTo(60, h * 0.78); g.lineTo(w - 60, h * 0.78); g.stroke();
    g.setLineDash([]);
    g.fillStyle = 'rgba(255,179,71,0.7)'; g.font = 'bold 34px system-ui';
    g.textAlign = 'center'; g.fillText('TIDEFORGE', w / 2, h / 2 + 12);
  }, 1024, 768);
  floorTex.wrapS = floorTex.wrapT = THREE.ClampToEdgeWrapping;
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(64, 48), new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.92, metalness: 0.15 }));
  floor.rotation.x = -Math.PI / 2;
  group.add(floor);

  // Perimeter walls.
  box(-32, -24, 32, -23, 4.2, steel); box(-32, 23, 32, 24, 4.2, steel);
  box(-32, -24, -31, 24, 4.2, steel); box(31, -24, 32, 24, 4.2, steel);
  // Interior: furnace block at Site A, pump tanks at Site B, mid structure.
  box(16, -18, 24, -14.5, 3.4, furnaceMat);       // A furnace mass (north strip; site floor stays open)
  box(24.5, -17, 27.5, -11, 2.2, rustMat);           // A side boiler
  box(16, 10, 24, 12.5, 2.6, tealMat);               // B pump housing (north strip; site floor stays open)
  decoBox(20, 14, 5, 5, 1.1, waterMat);              // B water tank (low, shoot over)
  box(-4, -3, 4, 3, 2.4, steelDark);                 // mid reactor core
  decoBox(0, 0, 6, 5, 0.4, rustLight, 2.4);          // core cap
  // Lane cover: crates, pillars, pipe runs.
  box(-14, -19.5, -11, -16.5, 1.3, crateMat);        // north lane crate
  box(2, -19.5, 5, -16.5, 1.3, crateDark);
  box(-14, 16.5, -11, 19.5, 1.3, crateDark);         // south lane crate
  box(2, 16.5, 5, 19.5, 1.3, crateMat);
  box(-12, -1.2, -9, 1.2, 3.0, steel);               // mid-west pillar wall
  box(9, -1.2, 12, 1.2, 3.0, steel);                 // mid-east pillar wall
  box(-2, -12, 2, -9, 1.2, crateMat);                // mid-north crate
  box(-2, 9, 2, 12, 1.2, crateDark);                 // mid-south crate
  box(13, -4, 15, -2, 1.4, crateMat);                // A approach crate
  box(13, 2, 15, 4, 1.4, crateDark);                 // B approach crate
  // Pipes along the north and south walls (visual only, high up).
  for (const z of [-22.4, 22.4]) {
    const pipe = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.35, 62, 10), pipeMat);
    pipe.rotation.z = Math.PI / 2; pipe.position.set(0, 3.6, z);
    group.add(pipe);
  }
  // Barrels (visual, no collision — small).
  for (const [x, z] of [[-24, -8], [-24, 8], [24, -6], [24, 6], [-6, -20], [-6, 20]]) {
    cyl(x, z, 0.55, 1.1, rustMat);
  }
  // Catwalk over mid (visual bridge at 3.4m, not walkable).
  decoBox(0, -6.5, 10, 1.6, 0.25, steelDark, 3.4);
  // Site markers: floating letter boards + ground rings.
  for (const key of ['A', 'B']) {
    const s = SITES_POS[key];
    const board = new THREE.Mesh(new THREE.PlaneGeometry(2.2, 2.2),
      new THREE.MeshBasicMaterial({ map: siteLabelTexture(key, key === 'A' ? '#ffb347' : '#2fd6a8'), transparent: true, side: THREE.DoubleSide }));
    board.position.set(s.x, 3.4, s.z);
    group.add(board);
    const ring = new THREE.Mesh(new THREE.RingGeometry(s.r - 0.25, s.r, 40),
      new THREE.MeshBasicMaterial({ color: key === 'A' ? 0xffb347 : 0x2fd6a8, transparent: true, opacity: 0.55, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2; ring.position.set(s.x, 0.03, s.z);
    group.add(ring);
  }
  // Sodium lamps: emissive fixtures + a few real point lights.
  const lampSpots = [[-20, 0], [0, -12], [0, 12], [20, -14], [20, 14], [28, 0], [-28, 0]];
  for (const [x, z] of lampSpots) decoBox(x, z, 1.4, 0.5, 0.18, lampMat, 3.9);
  for (const [x, z, c, i] of [[-20, 0, 0xffb347, 20], [0, 0, 0xffc46b, 24], [20, -14, 0xff7a2a, 24], [20, 14, 0x2fd6a8, 20]]) {
    const l = new THREE.PointLight(c, i, 24, 1.6);
    l.position.set(x, 3.6, z);
    group.add(l);
  }
  const hemi = new THREE.HemisphereLight(0x8fb8c0, 0x1a2024, 1.15);
  scene.add(hemi);
  const dir = new THREE.DirectionalLight(0xbfd8e0, 0.9);
  dir.position.set(-14, 22, -10);
  scene.add(dir);
  scene.fog = new THREE.FogExp2(0x10181c, 0.016);
  scene.background = new THREE.Color(0x10181c);

  return { colliders, waypoints: WAYPOINTS, sites: SITES_POS, spawns: SPAWNS };
}

export { SITES, WAYPOINTS };
