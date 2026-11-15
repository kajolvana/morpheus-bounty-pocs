# TIDEFORGE — 5v5 tactical FPS (threejs-fps-02)

A round-based tactical first-person shooter in the style Counter-Strike made
famous, built on three.js 0.187.0, running from static files with no build
step and no network request. Single-player against nine bots.

**The game:** Harborjackets vs Foundry Wardens, 5v5, first to 7 rounds
(max 13, sides swap at halftime). Attackers plant the Tide Core at Furnace
Hall (Site A) or the Pump Gallery (Site B); defenders hold the sites and
defuse. Buy phase between rounds: money earned per round, kill, plant and
defuse buys rifles, SMGs, a shotgun, a scoped sniper, armor and grenades.

**Map:** Tideforge Foundry — a tidal-power foundry on a harbor pier. Three
lanes (North, Mid Concourse, South) between two plant sites, crates and
furnace blocks for cover, a reactor core at mid, sodium lamps and a teal /
rust palette. All geometry, textures and sounds are generated in code.

## How to play

```
node serve.ts            # serves game/ on http://localhost:8080
```

Open the page in desktop Chrome, click **Click to play**, and fight.
WASD move, mouse look, click fire, right mouse aim (the Longtide sniper
gets a real circular scope overlay), R reload, Ctrl crouch, Space jump,
Shift walk quietly, B buy menu, E hold to plant/defuse, 4/5/6 throw smoke /
flash / surge grenades.

## Files

- `game/index.html` — the page: import map for three.js, full HUD
  (health, armor, ammo, money, round timer, score, minimap, killfeed,
  radio, buy menu, plant bar, scope overlay).
- `game/src/main.js` — renderer, input, fixed-step loop, viewmodel gun.
- `game/src/game.js` — the match: phases, economy, plant/defuse, wins.
- `game/src/world.js` — the foundry map, collision, nav graph, sightlines.
- `game/src/bots.js` — bot AI: routes, holds, engages, plants, defuses,
  hears gunfire, throws smoke/flash, talks on the radio.
- `game/src/weapons.js` — hitscan gunplay: spread, recoil, headshots.
- `game/src/grenades.js` — smoke / flash / HE with arced throws.
- `game/src/player.js` — first-person controller: accel, crouch, jump.
- `game/src/hud.js` — HUD + minimap (redrawn at 8 Hz, not every frame).
- `game/src/audio.js` — all sounds synthesized with WebAudio.
- `game/src/config.js` — every balance number in one place.
- `game/vendor/three/` — three.js 0.187.0, byte for byte from the kit.
- `main.ts` — the entry program: copies game/ + screenshots/ to /output
  and writes dossier.json with vendor/makefun-threejs.ts.
- `screenshots/` — 4 real renders of this code (see shots.json).

## Screenshots

Staged deterministically with `?shot=` (seeded): `1-spawn.jpg` (spawn +
buy menu), `2-buy.jpg` (armoury), `3-play.jpg` (mid-round with smoke),
`4-plant.jpg` (planting the Core at Furnace Hall).

## License

MIT — see LICENSE.
