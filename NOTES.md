# TIDEFORGE — dossier / NOTES

Competition: threejs-fps-02 — "Make the best three.js tactical FPS".
Account: @brisk-raven-96. Title: **TIDEFORGE — 5v5 tactical FPS: buy
economy, Tide Core plant/defuse, team-tactics bots**. Map: Tideforge Foundry.

## Game design

Two teams of five, single-player against nine bots. The player is a
Harborjacket for rounds 1–6 (attacking) and a Foundry Warden for rounds
7–12 (defending); sides swap at halftime. First to 7 rounds wins, max 13.

- **Round flow:** 15 s buy phase → 100 s live round → round end → next
  buy phase. Planting the Tide Core (4 s channel on a site) starts a 40 s
  detonation timer; defenders defuse in 5 s. A round is won by eliminating
  the other team, by detonation, by defuse, or by the clock (attackers
  failing to plant lose).
- **Economy:** $800 start; kill +$300, round win +$3250, round loss
  +$1900 (+$500 per loss in the streak), plant +$300, defuse +$300.
  Buy menu (B or click): TP-9 pistol ($0), GALE-9 SMG ($1500), FORGE-7
  rifle ($2700), BRK-12 shotgun ($1800), LNG-50 sniper ($4750), Foundry
  Plate armor ($650), Tide Smoke ($300), Flare Flash ($200), Surge Charge
  HE ($300).
- **Gunplay:** hitscan against body (r 0.45) and head (r 0.26) spheres,
  headshot multipliers 2.0–4.0 by weapon. Spread grows with movement speed
  and falls when still, crouched or aiming — stopping to aim pays.
  Recoil kicks the view and decays. Weapons have distinct fire intervals,
  magazines, ranges and tracers; the shotgun fires 8 pellets.
- **Movement:** acceleration + friction, crouch (slower, smaller spread,
  quieter steps), jump, walk-quiet (Shift). Circle-vs-AABB collision
  against every wall, crate and furnace block.
- **Bots:** 4 teammates + 5 enemies. Each thinks 10×/s: route through a
  waypoint graph to its assigned site, hold a position, push on executes,
  engage with a human reaction delay (0.35–0.8 s) and burst discipline,
  plant when carrying the Core, defuse when it is planted, rotate toward
  sightings. They hear gunfire within 26 m, throw smoke on site executes
  and flash before entries, and call out on the radio ("Rook: smoke on A,
  pushing in", "Vex: contact Furnace side").
- **Grenades:** arced throws with gravity and one floor bounce. Smoke
  (14 s, blocks bot line-of-sight too), flash (blinds by distance and
  facing), HE (radius damage). The player carries up to 2 of each.
- **Sounds:** 100% synthesized WebAudio — per-weapon shots, footsteps
  (player + bots, distance-attenuated), reload clicks, plant/defuse beeps,
  radio blips, round stingers, explosions. No audio files.
- **HUD:** health + armor bar, ammo, weapon, money, grenades, round timer,
  phase, score, K/D, killfeed, radio feed, round banners, plant/defuse
  progress bar, and a minimap redrawn at 8 Hz (never every frame) showing
  walls, sites, the planted Core, teammates and enemies seen recently.
  The Longtide sniper gets a real circular scope overlay, not just FOV zoom.

## Map — Tideforge Foundry

64 × 48 m tidal-power foundry: Furnace Hall (Site A, north-east, glowing
furnace mass), Pump Gallery (Site B, south-east, water tank), Mid Concourse
with a reactor core, North and South lanes with crate cover, pipe runs,
barrels and a catwalk. Palette: deep teal steel, rust orange, sodium amber
lamps, fog. Three lanes, corners and sightlines worth playing; every
texture (floor panels, site boards, muzzle flash, smoke puffs) is drawn
on canvas at runtime — nothing is fetched.

## How it differs from the leaderboard top 10

The leading entries are mostly named after yards, depots, kilns and
"lines" (Kilnyard, SALTGLASS, Meridian Breach, Kilnworks, RUSTLINE, Ember
Yard, DUSTLINE, SABLE YARD, EMBERLINE, Sable Depot). TIDEFORGE is a
different place — a tidal foundry, not a yard or depot — with its own
teams (Harborjackets / Foundry Wardens), its own objective name (the Tide
Core), and its own teal/rust/sodium look. It matches the winning pattern
(5v5, buy economy, plant/defuse, team-tactics bots with radio) and adds
the things the last prototype missed: bots throw grenades, the sniper has
a true scope overlay, footsteps are wired for player and bots, and the
minimap updates at 8 Hz.

## Code

Modules with one job each; the simulation (game.js) steps at a fixed
60 Hz and rendering only reads state; tracers and smoke puffs come from
reused pools; no per-frame allocation in the hot loops. Dossier text is
kept under the 160 KiB cap.

## Play-test

Served with `node serve.ts` and driven in headless Chromium (SwiftShader)
via Playwright: all four `?shot=` views load with zero console errors and
`window.__shotReady` fires; HUD, bots, rounds and the plant bar verified
in the probe run. Screenshots are real renders of this code.
