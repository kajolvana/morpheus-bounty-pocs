# NOTES — free-model-aggregator-02, @brisk-raven-96

## Routing strategy

Quality-ranked live router, frugal by design: 1 gateway call per prompt
when nothing refuses, at most 3 attempts. Requirements are read off the
request body itself (response_format → JSON, a tools list → tools, image
parts → vision) instead of trusting prompt arrival order, so any of the
three practice workloads — and the unseen scored ones — route correctly.

Per prompt, candidates are the free models meeting the requirements, sorted
by the brief's house rating (ties keep brief order). That gives the best
model the scorer credits for each prompt: Super VL (1303) for the two
JSON-mode prompts, Inkling 2 Small (1475) for the rest. Two guards protect
the score beyond the ranking:

1. **Token guard.** The prompt's unique token (WORD-1234) is extracted from
   the request text; an answer that does not carry it is discarded and the
   next model is tried. A model that rambles, reasons without answering, or
   calls a tool instead of answering therefore costs one extra call, not
   the prompt.
2. **Buffered streams.** The gateway stream is collected whole, verified,
   then re-emitted as one canonical SSE sequence (OpenAI: content chunk,
   finish chunk with usage, `[DONE]`; Anthropic: message_start, text/tool
   blocks, message_delta, message_stop). The token check runs before the
   client sees anything, and the event order is exact on both wires.

Failures are reported on `x-makefun-upstream-failures` (max 8 rows of
{status, code, model}) so the failover column can see retried refusals.
Usage is passed through whole and synthesized from text length only when
the gateway omits it (the wire needs output ≥ 1). All reply timestamps are
fixed constants; no wall clock or unseeded randomness reaches any request,
so a replay leaves the same state export.

## How it differs from the leaderboard top 10

The top 10 all sit at 94 (quality 90, served/wire/failover/catalog 100,
6 upstream calls). Quality 90 is what you get when the non-JSON prompts
are served one notch below the best — e.g. Super VL for JSON and Nemotron
3 Ultra (1285) elsewhere, (2×1303 + 6×1285) / (2×1303 + 6×1475) ≈ 90%.
This entry instead aims for quality 100: best-per-prompt first choices
(Super VL for JSON, Inkling for the rest) with rating-ordered failover, so
it only degrades to ~93–96 when a first choice actually refuses, and it
keeps the same 6-call frugality when nothing refuses. It also differs in
implementation: body-derived requirements (no manifest-order counter),
token-guard failover on 200 answers (not only on refusals), and canonical
buffered streams instead of relayed gateway chunks.

## Local verification (mock gateway — no live key)

The kit's real env (`program/env/server.mjs`) and sealed scorer
(`program/scorer/score.mjs`) were run against this server with a local mock
gateway that answers exactly like the real one (provider slug without the
`openrouter/` prefix, usage, SSE) and echoes each prompt's token. A
cassette was built from the mock's call log for the scorer.

| trial | mode | score | quality | served | wire | failover | catalog | calls |
|-------|------|-------|---------|--------|------|----------|---------|-------|
| 0 | ok | 100 | 100 | 100 | 100 | 100 | 100 | 6 |
| 0 | refuse (inkling 429×2, super-vl 500×1) | 96 | 93 | 100 | 100 | 100 | 100 | 6 |
| 0 | notoken (inkling answers without the token) | 98 | 96 | 100 | 100 | 100 | 100 | 7 |
| 0 | toolcall (tools prompt answered by a tool call) | 99 | 98 | 100 | 100 | 100 | 100 | 7 |
| 1 | ok | 100 | 100 | 100 | 100 | 100 | 100 | 6 |
| 2 | ok | 100 | 100 | 100 | 100 | 100 | 100 | 6 |
| 0 | nokey (every call 401) | 5 | 0 | 0 | 0 | 0 | 100 | 0 |

All 39 env checks pass on every valid run; `real_model` passes. The
state export (`state.json`) is byte-identical across three runs of trial-0
ok, and `--list` output is byte-identical across runs and identical to the
catalog registered with the env.

## What a live key would still need to prove (NOT tested)

- That Inkling 2 Small and Super VL answer a one-token prompt on the real
  OpenRouter free tier within the 30 s client window, and that their text
  (not a reasoning field) carries the token. If Inkling is slow or
  refuses live, this server fails over to Super VL / Ultra and lands
  around quality 90–93 — the leaderboard's current level.
- Real 429/5xx shapes and rate-limit timing from OpenRouter (the mock's
  refusals are scripted, not observed).
- The 10-credit account's 1,000/day free ceiling vs the 50/day ceiling
  below 10 credits (6 calls per trial, so either ceiling covers a trial;
  the 10-credit purchase is a competition requirement, not a capacity one).

## The blocker

**OpenRouter API key (paid requirement, NOT done).** This competition
requires an OpenRouter API key from an account with at least 10 credits
purchased (~$10 minimum), pushed to the competition before submitting.
No key was bought, no key is in this repository, and nothing here pushes
one. The `nokey` row above is the proof of what happens without it:
every gateway call answers 401, every prompt is lost, the trial scores 5.
The parent must ask kajol whether to buy the credits; only then can this
entry be submitted and scored live.
