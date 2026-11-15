// audio.js — every sound is synthesized with WebAudio: no audio files,
// no network. A tiny synth per effect; master gain + distance attenuation
// for world sounds. Created lazily on the first user gesture.
import { clamp } from './config.js';

let ctx = null;
let master = null;
let noiseBuf = null;

export function ensureAudio() {
  if (ctx) { if (ctx.state === 'suspended') ctx.resume(); return; }
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return;
  ctx = new AC();
  master = ctx.createGain();
  master.gain.value = 0.5;
  master.connect(ctx.destination);
  const len = ctx.sampleRate * 1;
  noiseBuf = ctx.createBuffer(1, len, ctx.sampleRate);
  const d = noiseBuf.getChannelData(0);
  for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
}

function noise(dur, filterType, freq, q, gain, when = 0, sweepTo = 0) {
  if (!ctx) return;
  const t = ctx.currentTime + when;
  const src = ctx.createBufferSource();
  src.buffer = noiseBuf;
  src.loop = true;
  const f = ctx.createBiquadFilter();
  f.type = filterType; f.frequency.setValueAtTime(freq, t); f.Q.value = q;
  if (sweepTo) f.frequency.exponentialRampToValueAtTime(Math.max(40, sweepTo), t + dur);
  const g = ctx.createGain();
  g.gain.setValueAtTime(gain, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + dur);
  src.connect(f); f.connect(g); g.connect(master);
  src.start(t); src.stop(t + dur + 0.05);
}

function tone(freq, dur, type, gain, when = 0, slideTo = 0) {
  if (!ctx) return;
  const t = ctx.currentTime + when;
  const o = ctx.createOscillator();
  o.type = type; o.frequency.setValueAtTime(freq, t);
  if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur);
  const g = ctx.createGain();
  g.gain.setValueAtTime(gain, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + dur);
  o.connect(g); g.connect(master);
  o.start(t); o.stop(t + dur + 0.05);
}

// Distance-attenuated world sound: gain falls off with distance (metres).
function worldGain(dist) { return clamp(1 - dist / 55, 0.05, 1); }

export const sfx = {
  shot(kind, dist = 0) {
    if (!ctx) return;
    const g = worldGain(dist);
    if (kind === 'pistol') { noise(0.09, 'bandpass', 1400, 1, 0.5 * g, 0, 300); tone(160, 0.08, 'square', 0.22 * g, 0, 60); }
    else if (kind === 'smg') { noise(0.07, 'bandpass', 1800, 1, 0.42 * g, 0, 400); tone(190, 0.06, 'square', 0.16 * g, 0, 70); }
    else if (kind === 'rifle') { noise(0.12, 'bandpass', 1100, 0.8, 0.6 * g, 0, 220); tone(120, 0.11, 'square', 0.3 * g, 0, 45); }
    else if (kind === 'shotgun') { noise(0.22, 'lowpass', 900, 0.7, 0.75 * g, 0, 150); tone(90, 0.16, 'square', 0.35 * g, 0, 35); }
    else if (kind === 'sniper') { noise(0.3, 'bandpass', 900, 0.7, 0.8 * g, 0, 120); tone(75, 0.25, 'sawtooth', 0.4 * g, 0, 30); }
  },
  dryFire() { tone(900, 0.03, 'square', 0.12); },
  reload() { tone(1400, 0.03, 'square', 0.1); tone(900, 0.03, 'square', 0.12, 0.18); tone(1600, 0.03, 'square', 0.12, 0.45); },
  // Footstep: short filtered thud. Volume scales with speed; bots pass dist.
  step(dist = 0, run = true) {
    if (!ctx) return;
    const g = worldGain(dist) * (run ? 0.16 : 0.08);
    noise(0.07, 'lowpass', 320 + Math.random() * 120, 1, g, 0, 120);
  },
  hit() { tone(2200, 0.05, 'sine', 0.18); },
  headshot() { tone(2600, 0.07, 'sine', 0.22); tone(3200, 0.05, 'sine', 0.14, 0.05); },
  kill() { tone(520, 0.09, 'triangle', 0.2); tone(780, 0.12, 'triangle', 0.2, 0.08); },
  hurt() { noise(0.12, 'lowpass', 500, 1, 0.3, 0, 150); tone(140, 0.12, 'sawtooth', 0.12, 0, 70); },
  bounce() { tone(500, 0.05, 'square', 0.1, 0, 240); },
  pin() { tone(3200, 0.03, 'square', 0.1); },
  explosion(dist = 0) {
    if (!ctx) return;
    const g = worldGain(dist);
    noise(0.7, 'lowpass', 700, 0.6, 0.9 * g, 0, 60);
    tone(60, 0.6, 'sine', 0.5 * g, 0, 28);
  },
  plantBeep(final = false) { tone(final ? 1500 : 1100, final ? 0.2 : 0.08, 'square', 0.14); },
  defuseBeep() { tone(950, 0.08, 'square', 0.14); tone(1250, 0.08, 'square', 0.14, 0.1); },
  // Radio blip that precedes a bot callout line.
  radio() { tone(1900, 0.04, 'square', 0.07); tone(1500, 0.04, 'square', 0.07, 0.06); },
  buy() { tone(880, 0.06, 'triangle', 0.15); tone(1320, 0.09, 'triangle', 0.15, 0.07); },
  roundStart() { tone(660, 0.12, 'triangle', 0.18); tone(880, 0.18, 'triangle', 0.18, 0.12); },
  roundWin() { [523, 659, 784, 1047].forEach((f, i) => tone(f, 0.16, 'triangle', 0.18, i * 0.11)); },
  roundLose() { [392, 330, 262].forEach((f, i) => tone(f, 0.2, 'triangle', 0.16, i * 0.14)); },
  scope() { tone(2400, 0.03, 'sine', 0.08); },
  flash() { tone(3000, 0.4, 'sine', 0.12, 0, 800); },
};
