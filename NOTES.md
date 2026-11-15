# NOTES — Quality-99 Floor Router

## The problem

Twenty requests, ten providers, hidden per-trial budgets, hidden failure
schedules (a 402 on one provider, 429s on the other nine, some with a
retry-after), and one outage. The quality denominator is the best legal
rating per request ignoring budgets, so quality 99 needs a weighted
deficit of at most 382 milli against the per-request best (1475 for most
requests, 1458 for the three vision ones). The top models are
concentrated on five providers (OpenRouter 1475, Z.ai / Cloudflare 1470,
SambaNova 1465, Gemini 1458), whose combined budgets are only ~30 calls
— so a router that always takes the single best model hammers one
provider until its failure schedule bites, and a router that spreads
evenly lands on 1390–1434 models and caps out at quality 98.

## The strategy

**Usage-penalized quality.** Candidates are ordered by

```
score = rating_milli - 8 * (calls already made to that provider)
```

ties broken by provider id, then model id. The penalty is small enough
that a 1475 model stays first while its provider is fresh, and large
enough that after two or three calls to OpenRouter the 1470 models on
Z.ai and Cloudflare and the 1465 on SambaNova overtake it — before the
failure ordinals (which start at call 2) are hit repeatedly. Weight-2
requests come first in the manifest, so they are served while every
provider is still cheap and get the best models; weight-1 requests take
the slightly penalized tail.

A provider that refuses is skipped for the rest of that request only
(the next candidate is tried immediately); it is not blacklisted, because
a scheduled failure is a single call ordinal, not exhaustion, and the
provider usually answers the next call. Exactly one provider call ever
answers a request, which the scorer's `one_provider` check requires.

Requirements are read twice and merged: the manifest row (requests arrive
in manifest order) plus signals from the body — tools, an image block,
JSON mode, user text over 5000 characters (long context). JSON-mode
requests on the OpenAI wire never go to an Anthropic-wire provider, whose
wire cannot echo JSON mode.

## How this differs from the leaderboard top

- The 20-call floor routers (the bulk of the score-99 board) cap each
  provider below its first failure ordinal and never pay a failure. That
  buys the tie-break (20 calls) but caps quality at 98: with zero
  failures the tail of the workload has to use 1390–1434 models. This
  router pays ~6–9 failed calls to keep the tail on 1465–1475 models,
  reaching quality 99 — the score is 99 either way, but on the three
  fresh scored instances (new failure schedules nobody can precompute)
  the zero-failure caps cannot be known in advance, while the penalty
  adapts online.
- A pure rating-desc router (quality-first) also reaches quality 99 but
  spends 30–36 calls on the practice trials and up to 84 on adversarial
  draws, because it re-hammers the best provider through its whole
  failure schedule. The 8-milli penalty cuts that to 26–29 calls with
  the same quality.
- A value router (`rating / (used + 1)`) penalizes use too aggressively
  and drops to quality 98; a min-cost-flow assignment needs the budgets
  up front, which the brief deliberately withholds. The linear penalty
  was tuned by simulation over the three practice trials plus 30 random
  instances drawn from the sealed budget/failure ranges: penalty 8 holds
  quality 99 in 30/30 random draws (mean 26.5 calls); penalties 4 and
  15 also hold quality but cost more calls or slip on some draws.

## What was verified locally

- Kit simulator + scorer on all three practice trials: score 99,
  quality 99, served 100, wire 100, failover 100, catalog 100,
  27/29/26 upstream calls, 20/20 valid.
- `--list` output is byte-identical across runs and matches the
  reference catalog's free sets on all three trials.
- Two full runs of trial 0 leave byte-identical state exports
  (deterministic replay).
