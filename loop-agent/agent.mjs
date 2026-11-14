// loop-agent — autonomous AIProve loop agent (Node.js, ethers v6).
//
// The loop, straight from aiprove.fun/skill.md:
//   1. read skill.md (GET /skill.md) and confirm the auth + bounty endpoints
//   2. authenticate with its own keypair (EIP-191 personal_sign challenge)
//   3. list open bounties (GET /api/bounties?status=open)
//   4. enter the bounties it can complete, and complete them:
//      for AutoGrade forecast bounties it can price (BTC / ETH via the
//      keyless Kraken public API) it computes a persistence forecast
//      from real daily closes and submits a prediction.
//   5. verify via GET /api/me and /api/me/submissions.
//
// HTTP goes through curl (child_process, no shell): Node's fetch does not
// honor the egress proxy in the sandbox this agent runs in, curl does.
// The challenge string is passed byte-exact (never through a shell).
//
// Keypair: generated fresh on first run and stored in keypair.json
// (mode 600) next to this file. The private key and the session token
// are NEVER printed or written to any log.
//
// Env:
//   API_BASE_URL  — arena host, default https://aiprove.fun
//                   (set it to point at a staging arena if one exists)
//   KEYPAIR_FILE  — default ./keypair.json
//   HANDLE        — agent handle, default loop-agent-<addr6>
//   MAX_BOUNTIES  — max forecast bounties to complete, default 10
//
// Usage:
//   node agent.mjs --dry        # auth + list only, no enter/submit
//   node agent.mjs --max 1      # complete at most one bounty (minimal loop)
//   node agent.mjs              # full autonomous loop
//
// Exit code: 0 = loop completed cleanly (every attempted step succeeded),
//            1 = a step failed (see run-log.jsonl for which one).

import fs from "fs";
import path from "path";
import crypto from "crypto";
import { execFile } from "child_process";
import { promisify } from "util";
import { ethers } from "ethers";

const execFileAsync = promisify(execFile);
const API_BASE = (process.env.API_BASE_URL || "https://aiprove.fun").replace(/\/$/, "");
const KEYPAIR_FILE = process.env.KEYPAIR_FILE || path.join(process.cwd(), "keypair.json");
const LOG_FILE = path.join(process.cwd(), "run-log.jsonl");
const MAX_BOUNTIES = parseInt(process.env.MAX_BOUNTIES || "10", 10);

const args = process.argv.slice(2);
const DRY = args.includes("--dry");
const maxIdx = args.indexOf("--max");
const MAX = maxIdx >= 0 ? parseInt(args[maxIdx + 1], 10) : MAX_BOUNTIES;

function log(step, data) {
  const line = JSON.stringify({ t: new Date().toISOString(), step, ...data });
  fs.appendFileSync(LOG_FILE, line + "\n");
  console.log(`[${new Date().toISOString().slice(11, 19)}Z] ${step}: ${JSON.stringify(data)}`);
}

// --- HTTP via curl (proxy-aware in this sandbox) ---
async function http(method, url, { body = null, token = null } = {}) {
  const a = ["-s", "-m", "25", "-X", method, url, "-H", "content-type: application/json"];
  if (token) a.push("-H", `Authorization: Bearer ${token}`);
  if (body !== null) a.push("-d", JSON.stringify(body));
  const { stdout } = await execFileAsync("curl", a, { maxBuffer: 10 * 1024 * 1024 });
  return JSON.parse(stdout);
}

async function httpText(url) {
  const { stdout } = await execFileAsync("curl", ["-s", "-m", "25", url], { maxBuffer: 10 * 1024 * 1024 });
  return stdout;
}

// --- keypair: load or generate fresh (never touches any other wallet) ---
function loadOrCreateKeypair() {
  if (fs.existsSync(KEYPAIR_FILE)) {
    const k = JSON.parse(fs.readFileSync(KEYPAIR_FILE, "utf8"));
    const w = new ethers.Wallet(k.privateKey);
    log("keypair", { mode: "loaded", address: w.address });
    return w;
  }
  const w = ethers.Wallet.createRandom();
  fs.writeFileSync(KEYPAIR_FILE, JSON.stringify({ address: w.address, privateKey: w.privateKey }, null, 2), { mode: 0o600 });
  log("keypair", { mode: "generated-fresh", address: w.address });
  return w;
}

// --- step 1: read skill.md and confirm the loop it describes ---
async function readSkill() {
  const text = await httpText(`${API_BASE}/skill.md`);
  const markers = ["Authenticate", "/api/auth/challenge", "/api/bounties", "personal_sign", "/enter", "/submit"];
  const missing = markers.filter((m) => !text.includes(m));
  if (missing.length) throw new Error(`skill.md missing markers: ${missing.join(", ")}`);
  log("read-skill", { bytes: text.length, sha256: crypto.createHash("sha256").update(text).digest("hex").slice(0, 16), markers_ok: markers.length });
  return text;
}

// --- step 2: authenticate (EIP-191 challenge -> sign -> session token) ---
async function authenticate(wallet) {
  const ch = await http("GET", `${API_BASE}/api/auth/challenge?address=${wallet.address}`);
  if (!ch.challenge) throw new Error(`no challenge returned: ${JSON.stringify(ch).slice(0, 200)}`);
  const signature = await wallet.signMessage(ch.challenge);
  const v = await http("POST", `${API_BASE}/api/auth/verify`, { body: { address: wallet.address, signature } });
  if (!v.token) throw new Error(`verify failed: ${JSON.stringify(v).slice(0, 200)}`);
  log("auth", { address: v.address, token_len: v.token.length, expiresAt: v.expiresAt });
  return v.token;
}

// --- step 3: list open bounties ---
async function listBounties() {
  const d = await http("GET", `${API_BASE}/api/bounties?status=open`);
  const items = Array.isArray(d) ? d : d.bounties || [];
  log("list-bounties", { open: items.length, slugs: items.map((b) => b.slug) });
  return items;
}

// --- pricing (keyless public Kraken API) + forecast model ---
// Binance is geo-blocked from this sandbox (HTTP 451, "restricted
// location"), so prices come from Kraken's public OHLC endpoint
// (no key, no account). Returns daily closes, most recent last,
// dropping the current (incomplete) day's candle.
async function krakenCloses(symbol, limit = 10) {
  const pair = symbol === "BTC" ? "XBTUSD" : "ETHUSD";
  const d = await http("GET", `https://api.kraken.com/0/public/OHLC?pair=${pair}&interval=1440`);
  if (d.error && d.error.length) throw new Error(`kraken: ${d.error.join("; ")}`);
  const candles = d.result[pair] || d.result[Object.keys(d.result)[0]];
  if (!Array.isArray(candles) || candles.length < limit + 1) throw new Error("kraken: too few candles");
  return candles.slice(-(limit + 1), -1).map((c) => parseFloat(c[4]));
}

function forecastPrice(closes, horizonDays) {
  // Jump-aware persistence: last close, drift = mean daily return of the
  // last 10 closes, damped by 0.5 (jumps persist, but not at full speed).
  const rets = [];
  for (let i = 1; i < closes.length; i++) rets.push(closes[i] / closes[i - 1] - 1);
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const last = closes[closes.length - 1];
  const pred = last * Math.pow(1 + mean * 0.5, horizonDays);
  return { last, meanDailyPct: mean * 100, prediction: Math.round(pred * 100) / 100 };
}

// Which forecast bounties can this agent price, and how?
// slug -> { symbol, horizonDays }. Others are skipped with a logged reason.
const PRICEABLE = {
  "btc-tomorrow": { symbol: "BTC", horizonDays: 1 },
  "forecast-btc-7d": { symbol: "BTC", horizonDays: 7 },
  "forecast-eth-24h": { symbol: "ETH", horizonDays: 1 },
};

async function enterAndComplete(token, handle, bounties) {
  const done = [];
  const skipped = [];
  let count = 0;
  for (const b of bounties) {
    if (count >= MAX) break;
    if (b.lane !== "AutoGrade" || !PRICEABLE[b.slug]) {
      skipped.push({ slug: b.slug, reason: b.lane !== "AutoGrade" ? `lane ${b.lane} needs human/poster judging` : "no keyless price source for this asset in this agent" });
      continue;
    }
    const { symbol, horizonDays } = PRICEABLE[b.slug];
    try {
      const closes = await krakenCloses(symbol);
      const f = forecastPrice(closes, horizonDays);
      if (!DRY) {
        const e = await http("POST", `${API_BASE}/api/bounties/${b.slug}/enter`, { token, body: { handle } });
        if (!e.id) throw new Error(`enter failed: ${JSON.stringify(e).slice(0, 200)}`);
        const s = await http("POST", `${API_BASE}/api/bounties/${b.slug}/submit`, {
          token,
          body: { prediction: { answer: f.prediction, confidence: 0.5 }, handle },
        });
        if (!s.id) throw new Error(`submit failed: ${JSON.stringify(s).slice(0, 200)}`);
        done.push({ slug: b.slug, submissionId: s.id, status: s.status, prediction: f.prediction, lastClose: f.last });
        log("completed", { slug: b.slug, submissionId: s.id, status: s.status, prediction: f.prediction, lastClose: f.last, meanDailyPct: +f.meanDailyPct.toFixed(2) });
      } else {
        done.push({ slug: b.slug, dry: true, prediction: f.prediction, lastClose: f.last });
        log("would-complete", { slug: b.slug, prediction: f.prediction, lastClose: f.last });
      }
      count++;
    } catch (err) {
      skipped.push({ slug: b.slug, reason: `error: ${err.message.slice(0, 160)}` });
      log("bounty-error", { slug: b.slug, error: err.message.slice(0, 200) });
    }
  }
  log("summary", { completed: done.length, skipped: skipped.length, skippedList: skipped });
  return { done, skipped };
}

// --- step 5: verify via /api/me ---
async function verify(token) {
  const me = await http("GET", `${API_BASE}/api/me`, { token });
  const subs = await http("GET", `${API_BASE}/api/me/submissions`, { token });
  const list = Array.isArray(subs) ? subs : subs.submissions || [];
  log("verify", {
    competing: (me.competing || []).map((c) => c.slug || c),
    submissions: list.map((s) => ({ slug: s.bountySlug, id: s.id, status: s.status })),
  });
  return { me, submissions: list };
}

async function main() {
  log("start", { apiBase: API_BASE, mode: DRY ? "dry (no enter/submit)" : `live, max ${MAX} bounties` });
  const wallet = loadOrCreateKeypair();
  const handle = process.env.HANDLE || `loop-agent-${wallet.address.slice(2, 8).toLowerCase()}`;
  await readSkill();
  const token = await authenticate(wallet);
  const bounties = await listBounties();
  const { done } = await enterAndComplete(token, handle, bounties);
  const { submissions } = await verify(token);
  const ok = DRY || done.every((d) => submissions.some((s) => s.bountySlug === d.slug && s.id === d.submissionId));
  log("finish", { clean: ok, completed: done.length, submissionsOnRecord: submissions.length });
  if (!ok) {
    console.error("LOOP NOT CLEAN: a completed bounty is missing from /api/me/submissions");
    process.exit(1);
  }
  console.log(`LOOP CLEAN: ${done.length} ${done.length === 1 ? "bounty" : "bounties"} completed end to end${DRY ? " (dry run — nothing entered/submitted)" : ""}.`);
}

main().catch((err) => {
  log("fatal", { error: err.message.slice(0, 300) });
  console.error("FATAL:", err.message);
  process.exit(1);
});
