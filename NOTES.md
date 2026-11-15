# Routing strategy — catalog-accuracy router

## The idea in one paragraph

Most of the score is decided before a single request is served: by whether
the router ever sends a request to a model that cannot serve it, and by
whether its first pick survives the provider's unpublished failure schedule.
So this entry invests in the catalog and in the plan, not in clever retries.
It parses the brief's quality table as the truth about capabilities, probes
every provider's catalog route to learn the free flag of every model it
lists (paid ones included), and plans the whole 20-request workload up front
with a min-cost flow. Failover exists, but it is a safety net for the plan,
not the plan itself.

## 1. Exact capability fit (the catalog-accuracy part)

- The brief's three documents are parsed by schema: provider directory,
  quality table (rating + tools/vision/JSON/context + free flag), workload
  manifest.
- Each provider's `GET /v1/models` (or `/v1beta/models` for Gemini) is
  probed in the provider's own wire and auth scheme. The catalog lists every
  model a provider lists, with the free flag from the quality table where it
  has a row, else from the listing itself — so the one paid model
  (`anthropic/claude-opus-5.5` on OpenRouter) is reported paid and never
  routed, and the 26 free models are all reported free with the right
  context lengths.
- A candidate must be free AND meet every requirement: tools, vision, JSON
  mode, minimum context. An unknown capability never meets a requirement.
- Wire-compatibility is part of capability: a JSON-mode OpenAI request is
  never sent to an anthropic-wire provider, because that wire cannot carry
  `response_format` and its echo would come back `json_mode: false` against
  a client that asked for JSON. Gemini carries JSON via
  `responseMimeType: application/json`; Anthropic requests never set it,
  because their echoed `json_mode` is always false.

## 2. The planned assignment (spread-first, min-cost flow)

At startup the router solves one assignment: all 20 manifest requests to
(free model, provider) pairs, maximising the weight-weighted rating under
per-provider call caps:

```
groq 5, cerebras 1, gemini 3, zai 1, openrouter 1, sambanova 1,
pollinations 4, cloudflare 1, ovh 1, kilol 5
```

Why caps at all: every provider's first call is the only one that is
certainly safe — each provider draws its own per-trial budget and failure
schedule, never published, and every failure costs an extra upstream call.
The caps spread the load so no provider is driven into its failure schedule,
and they leave spare capacity (notably on Pollinations and Kilo) for
failover retries to land on. While serving, a request prefers its planned
pick; the remaining candidates rank by rating minus 20 milli-points per
call already made to that provider.

The caps were tuned against the kit's three public practice instances and
ten fresh generator draws (not against any other entrant's entry): the
practice instance is public by design, and `grade-local` exists to tune
against it.

## 3. Failover by refusal kind

A 402, 429, 5xx or transport failure retries at once on the next-best
eligible candidate from a different provider; each provider is tried at
most once per request. The refusal's own message is read to classify it:

- "budget is spent" / "is out" → the provider is spent for the trial and is
  banned from later rankings.
- "refused this call" (or anything else) → a failure-schedule refusal: the
  provider stays available, because the schedule is per call number and its
  next call usually answers. The plan's later picks to it stand.

This is deliberately different from "drop a provider on its first refusal":
a single scheduled refusal does not throw away the rest of that provider's
budget, which the plan was counting on.

## How this differs from the top leaderboard entries

The leaderboard's top routers (quality 98, 20-call floor) are described as
fixed-plan / zero-failure floor routers that drop a provider on its first
refusal. This entry shares the floor goal but gets there differently:

- **Catalog-first, not plan-first.** The top entries' edge is a plan that
  never fails; this entry's edge is the catalog and the capability match
  that the plan is built on — exact free flags from probed listings,
  wire-compatibility as a routing constraint, and the paid model excluded
  by the probe rather than by a hardcoded list.
- **Refusal classification instead of drop-on-first-refusal.** A scheduled
  refusal does not ban the provider here; only a spent budget or an outage
  does. The plan survives a transient refusal instead of being rewritten
  around it.
- **Plan computed by optimisation, not by hand.** The assignment is a
  min-cost flow solved at runtime from the brief, so it follows the
  manifest, the ratings and the catalog wherever they point — it is not a
  fixed request→provider table written into the source.

## Test results (kit's own env + scorer, run locally)

| instance | score | quality | served | wire | failover | catalog | calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| practice trial-0 | 99 | 98 | 100 | 100 | 100 | 100 | 20 |
| practice trial-1 | 99 | 98 | 100 | 100 | 100 | 100 | 22 |
| practice trial-2 | 99 | 98 | 100 | 100 | 100 | 100 | 22 |
| 10 fresh draws | 99 | 98 | 100 | 100 | 100 | 100 | 20–29 (mean 23.3; 22.9 across all 13 instances) |

233/233 response checks passed on every practice trial. `--list` output is
byte-identical across runs and identical to the registered catalog; two full
runs of trial-0 leave byte-identical state exports (replay-safe).
`grade-local --driver process` cannot run a Python bot on this machine (the
process sandbox runs Node 22 only; Python 3.13 needs Docker, which is not
installed here), so the trials above were run directly against the kit's
`program/env/server.mjs` and scored with `program/scorer/score.mjs`.
