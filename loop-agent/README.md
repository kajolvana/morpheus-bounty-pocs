# loop-agent — autonomous AIProve loop agent

A single Node.js program that runs the full AIProve loop with **no human
in the loop**: it reads `aiprove.fun/skill.md`, authenticates with its
own keypair, lists open bounties, enters the ones it can complete, and
completes them — then verifies the result against the board.

Built for two bounties (both close 15 Nov 2026 13:49 UTC):

- **`sample-agent-skillmd`** (0.12 META, AutoGrade, "First clean loop
  completion") — the loop itself is the entry: read skill.md →
  authenticate → list → enter one bounty, cleanly, end to end.
- **`best-autonomous-agent`** (0.3 GLD, AutoGrade, "Bounties completed
  autonomously") — the same loop run to completion over every bounty
  the agent can price on its own. In the verification run it completed
  **3 bounties** (see below); the rest were skipped with a logged
  reason, never faked.

## The loop

```
read skill.md ──> auth (EIP-191 challenge) ──> list open bounties
   ──> enter + submit each priceable forecast bounty ──> verify via /api/me
```

1. **Read skill.md** (`GET /skill.md`). The agent asserts the doc
   contains the auth section and the endpoint paths
   (`/api/auth/challenge`, `/api/bounties`, `personal_sign`,
   `/enter`, `/submit`) before doing anything else.
2. **Authenticate.** `GET /api/auth/challenge?address=` → sign the
   challenge with the agent's own keypair (ethers `signMessage`,
   EIP-191) → `POST /api/auth/verify` → Bearer session token
   (kept in memory only, never logged).
3. **List** open bounties (`GET /api/bounties?status=open`).
4. **Enter + complete.** For each open AutoGrade forecast bounty the
   agent can price, it fetches real daily closes from the keyless
   Kraken public OHLC API, computes a jump-aware persistence forecast
   (last close, drift = mean daily return of the last 10 closes,
   damped by 0.5), enters the bounty and submits the prediction.
   Bounties it cannot complete are skipped with a logged reason —
   PosterJudge bounties need human/poster judging, and code/trading
   bounties need a strategy repo, neither of which this agent fakes.
5. **Verify.** `GET /api/me` + `/api/me/submissions` — every
   completed bounty must appear on record, or the run exits 1.

## Setup

```bash
node --version   # v18+ (tested on v24.22.1)
npm install      # installs ethers v6
node agent.mjs --dry    # auth + list only, no enter/submit
node agent.mjs --max 1  # complete at most one bounty (minimal loop)
node agent.mjs          # full autonomous loop
```

Exit code 0 = loop completed cleanly (every attempted step succeeded
and verified on the board); 1 = a step failed (see `run-log.jsonl`).

## Keypair

On first run the agent generates a **fresh ephemeral keypair** and
stores it in `keypair.json` (mode 600) next to `agent.mjs`. The
private key and the session token are never printed or written to any
log. `keypair.json` is gitignored — the repo never contains a key.
Set `KEYPAIR_FILE` to use a different path. This agent never reads,
uses, or copies any other wallet, key, or token on the machine.

## Staging vs production

`API_BASE_URL` selects the arena host (default `https://aiprove.fun`).
No separate staging host is published anywhere: `staging.aiprove.fun`
does not resolve, and neither `skill.md`, the bounty details, nor the
`AIProveRH/aiprove` repo names a staging URL. The live API is itself
the prototype/staging environment (skill.md: "in this prototype chain
reads are stubbed … grading uses a deterministic sandbox stub"), so
the verification run below ran against it. If a staging host is ever
published, point `API_BASE_URL` at it — no code change needed.

## Verification run (14 Nov 2026, 14:24–14:25 WIB)

Fresh ephemeral identity `0x697f202831460F9B547d9a72C75E9fB35F414970`
(handle `loop-agent-697f20`). Loop completed cleanly; all 3
submissions verified on record via a fresh auth + `/api/me/submissions`:

| bounty | prediction | submission | status |
|---|---|---|---|
| `btc-tomorrow` | BTC $67,625.54 (last close $68,235.90) | `ad5beb58-be7f-4045-9242-94f3ab1e8120` | provenance_ok |
| `forecast-btc-7d` | BTC $64,076.34 (7-day damped drift) | `3752f05b-ef43-4e48-82ae-0d07798ddbfe` | provenance_ok |
| `forecast-eth-24h` | ETH $1,903.46 (last close $1,927.29) | `4474943a-eab3-4b33-875a-266afe648898` | provenance_ok |

19 of 22 open bounties skipped with reasons (PosterJudge lanes,
unpriceable assets, code/trading bounties needing a strategy repo).

## Limits found while building

- **Binance is geo-blocked from this sandbox** (HTTP 451,
  "restricted location"), so the first pricing attempt failed with
  `klines.map is not a function`. Pricing moved to Kraken's public
  OHLC endpoint (keyless, works here). CoinGecko also works but only
  gives sampled prices, not daily closes.
- **Node's `fetch` does not honor the egress proxy** in this sandbox,
  so all HTTP goes through `curl` (child_process, no shell). The
  challenge string is passed byte-exact; a trailing newline would
  break signature recovery.
- The agent prices BTC/ETH only. SPY, NVDA, gold, and the "top 5
  pons" forecast need price sources this agent does not bundle, so
  those bounties are skipped, not guessed.

## Files

- `agent.mjs` — the agent (single file, ethers v6)
- `package.json`, `package-lock.json` — deps (ethers only)
- `README.md` — this file
- `transcript.txt` — truthful build transcript (steps, timestamps,
  verification results; no secrets)
- `.gitignore` — `keypair.json`, `run-log.jsonl`, `node_modules/`
