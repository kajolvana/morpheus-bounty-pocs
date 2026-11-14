// fastest-primes entry: hybrid prime counter.
// Reads integer N (1 <= N <= 5,000,000) on stdin, prints pi(N) on stdout.
//
// Strategy (cold-start wall clock is the score, so the entry picks the
// cheaper algorithm per N):
//   N <= 200000 : segmented odd-only sieve + SWAR byte-sum count.
//                 (sieve compute is ~0-2 ms here; Lehmer's fixed setup
//                  of ~3 ms would cost more)
//   N >  200000 : Lehmer prime counting with a small pi table (MAXV=10000)
//                 and a periodic phi table. (compute ~3-4.5 ms cold,
//                  vs ~14 ms for the sieve at N=5,000,000)
// Crossover measured cold on this VM: sieve wins below ~200k, Lehmer above.
//
// Correctness: sieve branch verified against every public sample and a
// 82,390-value sweep (all N 1..20000, every 97th N to 5,000,000, values
// near squares/cubes/fourth powers +/-2, known checkpoints). Lehmer branch
// verified on the same 82,390 values with 0 mismatches, plus the public
// samples. pi(10)=4, pi(100)=25, pi(1000)=168, pi(100000)=9592,
// pi(1000000)=78498, pi(5000000)=348513.

const fs = require('fs');

const THRESHOLD = 200000;

// ---- segmented odd-only sieve (N <= THRESHOLD) ----
// comp[i] = 1 iff the odd number 2*i+1 is composite (i=0 is number 1).
// Count = size - (# of 1-bytes), computed with a SWAR byte-sum over the
// buffer viewed as Uint32 words, plus a tail loop. The +1 for prime 2 is
// exactly cancelled by index 0 (number 1, always 0), so the count is
// (number of zero bytes) = size - (number of one bytes).
function sieveCount(N) {
  const size = (N + 1) >> 1;
  const comp = new Uint8Array(size);
  const sq = Math.floor(Math.sqrt(N));
  const sSize = (sq + 1) >> 1;
  const sComp = new Uint8Array(sSize);
  const primes = [];
  for (let p = 3; p <= sq; p += 2) {
    if (!sComp[(p - 1) >> 1]) {
      primes.push(p);
      for (let j = ((p * p) - 1) >> 1; j < sSize; j += p) sComp[j] = 1;
    }
  }
  const B = 16384;
  const maxIdx = size - 1;
  for (let lo = 0; lo <= maxIdx; lo += B) {
    const hi = Math.min(lo + B, size);
    for (let k = 0; k < primes.length; k++) {
      const p = primes[k];
      const p2 = ((p * p) - 1) >> 1;
      const r = (p - 1) >> 1;
      let start = lo > p2 ? lo : p2;
      const rem = (((start - r) % p) + p) % p;
      const j0 = rem === 0 ? start : start + (p - rem);
      for (let j = j0; j < hi; j += p) comp[j] = 1;
    }
  }
  const w = new Uint32Array(comp.buffer, 0, size >> 2);
  let s = 0;
  for (let i = 0; i < w.length; i++) {
    const x = w[i];
    const y = (x & 0x00ff00ff) + ((x >>> 8) & 0x00ff00ff);
    s += (y & 0xffff) + (y >>> 16);
  }
  for (let i = w.length * 4; i < size; i++) s += comp[i];
  return size - s;
}

// ---- Lehmer prime counting (N > THRESHOLD) ----
// Lazily initialized on first use so small N never pays the setup cost.
const MAXV = 10000;
const PERIOD = 30030, PER_COUNT = 5760;
let L = null; // { piSmall, primes, smallPhi, phiMemo }
function lehmerInit() {
  const size = (MAXV + 1) >> 1;
  const comp = new Uint8Array(size);
  const sq = Math.floor(Math.sqrt(MAXV));
  for (let p = 3; p <= sq; p += 2) {
    if (!comp[(p - 1) >> 1]) {
      for (let j = ((p * p) - 1) >> 1; j < size; j += p) comp[j] = 1;
    }
  }
  const piSmall = new Uint32Array(MAXV + 1);
  const primes = [2];
  let c = 1;
  piSmall[2] = 1;
  for (let i = 3; i <= MAXV; i += 2) {
    piSmall[i - 1] = c;
    if (!comp[(i - 1) >> 1]) { c++; primes.push(i); }
    piSmall[i] = c;
  }
  if (MAXV % 2 === 0) piSmall[MAXV] = c;
  const smallPhi = new Uint16Array(PERIOD + 1);
  {
    const bad = new Uint8Array(PERIOD + 1);
    for (const p of [2, 3, 5, 7, 11, 13]) {
      for (let j = 0; j <= PERIOD; j += p) bad[j] = 1;
    }
    let cc = 0;
    for (let i = 0; i <= PERIOD; i++) {
      if (i > 0 && !bad[i]) cc++;
      smallPhi[i] = cc;
    }
  }
  L = { piSmall, primes, smallPhi, phiMemo: new Map() };
}
function phi(x, s) {
  if (s === 0) return x;
  if (s === 6) return Math.floor(x / PERIOD) * PER_COUNT + L.smallPhi[x % PERIOD];
  if (L.primes[s - 1] >= x) return 1;
  if (s < 6) return phi(x, s - 1) - phi(Math.floor(x / L.primes[s - 1]), s - 1);
  const key = x * 32 + s;
  const m = L.phiMemo.get(key);
  if (m !== undefined) return m;
  const v = phi(x, s - 1) - phi(Math.floor(x / L.primes[s - 1]), s - 1);
  L.phiMemo.set(key, v);
  return v;
}
function iroot4(x) {
  let r = Math.floor(Math.pow(x, 0.25));
  while (Math.pow(r + 1, 4) <= x) r++;
  while (Math.pow(r, 4) > x) r--;
  return r;
}
function isqrt(x) {
  let r = Math.floor(Math.sqrt(x));
  while ((r + 1) * (r + 1) <= x) r++;
  while (r * r > x) r--;
  return r;
}
function piLehmer(x) {
  if (x < MAXV) return L.piSmall[x];
  const a = piLehmer(iroot4(x));
  const b = piLehmer(isqrt(x));
  let sum = phi(x, a) + ((b + a - 2) * (b - a + 1)) / 2;
  for (let i = a; i < b; i++) {
    const w = Math.floor(x / L.primes[i]);
    sum -= piLehmer(w);
    const bi = piLehmer(isqrt(w));
    for (let j = i; j < bi; j++) sum -= piLehmer(Math.floor(w / L.primes[j])) - j;
  }
  return sum;
}
function lehmerCount(N) {
  if (!L) lehmerInit();
  return piLehmer(N);
}

// ---- main ----
const N = parseInt(fs.readFileSync(0, 'utf8'), 10);
let out;
if (N < 2) out = 0;
else if (N <= THRESHOLD) out = sieveCount(N);
else out = lehmerCount(N);
process.stdout.write(out + '\n');
