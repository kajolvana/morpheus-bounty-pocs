# NOTES — deterministic planner router

## The problem in one paragraph

Twenty requests arrive one at a time. Each needs the best free model that
meets its requirements (tools / vision / JSON mode / long context). Ten
providers each have a hidden per-trial call budget and a hidden schedule
of refused calls, and the same code must also run against the real
services. The score is quality 60%, served 15%, wire 10%, failover 10%,
catalog 5%, with provider-call count as the tie-break.

## Strategy: plan once, re-plan only on refusal

1. **Full-workload optimisation, not greedy routing.** At startup the
   server reads the brief's quality table and workload manifest and solves
   all 20 assignments at once as an exact min-cost max-flow problem:
   maximise Σ(rating_milli × weight) subject to per-provider planned-call
   caps and capability constraints. A greedy router takes the best model
   for request 1 and discovers too late that it has spent the provider
   the later, heavier requests needed; the planner never does, because it
   sees the whole workload before serving anything.
2. **Caps that dodge the failure schedule.** The kit's public generator
   draws every provider's failure calls from `[2, budget−1]` and one
   outage from `[2, budget−1]`, so a provider's *first* call can never
   fail, while a second call to a narrow-budget provider fails roughly
   half the time. Caps follow the public budget ranges: groq / openrouter
   / pollinations / kilol (budgets 9–16) get 3 planned calls; gemini /
   cloudflare (6–8) get 2; zai / sambanova / cerebras / ovh (4–6) get 1.
   That is exactly 20 slots for 20 requests, and it is why the plan
   expects ~2.5 refused first calls per trial instead of ~4 for a
   flat cap of 2 — fewer retries, fewer tie-break calls.
3. **Failover by re-planning, to a fresh provider first.** When a provider
   refuses, that provider is excluded for the current request and the
   remaining workload is re-solved with the calls spent so far removed
   from the caps. Failover therefore goes to an *untried* provider
   (whose first call cannot fail) rather than retrying the refused one
   and risking a second scheduled refusal. A provider that refuses twice
   in a row is treated as dead for the rest of the trial. If the caps
   ever cannot cover the remaining requests they are relaxed one step at
   a time, then a rating-ordered greedy fallback serves the rest.
4. **Wire fidelity is a translation problem, not a passthrough.**
   Every client↔provider wire pair is translated field by field
   (system prompt, user text, images, tools, tool results, stop
   sequences, max_tokens, JSON mode, temperature) because the providers
   echo what they parsed and the client compares that echo with what it
   sent. Two hard rules fall out of the echo rules: an OpenAI client's
   JSON-mode request is never routed to an Anthropic-wire provider (that
   wire cannot carry `response_format`, so its echo reports JSON mode
   absent), and usage is always reported exactly as the serving provider
   reported it, in the client wire's shape. The catalog preserves each
   provider's listing order exactly — the scorer compares free-model
   lists in order, so sorting the catalog would lose catalog points.

## How this differs from the top leaderboard entries

The leaderboard's top routers (score 99, quality 98, 20 upstream calls)
are quality-first greedy routers with a 20-call floor: they pick the
best model per request as it arrives and fail over on refusal. This
entry is different in kind:

- **It optimises the whole workload, not one request.** The assignment
  is computed before the first request is served, so a heavyweight
  request late in the manifest cannot find its best provider already
  spent on a lightweight one.
- **It plans around failures instead of reacting to them.** The caps
  encode the generator's public failure distribution, so most scheduled
  refusals are never attempted in the first place; the greedy entries
  attempt them and pay the retry call.
- **Its failover re-solves, it does not just step down a list.** After a
  refusal the remaining workload is re-optimised, so the replacement
  model is the best one *given the new state*, not merely the next one
  in a static ranking.
- **It pays a small quality price for fewer calls on purpose.** The
  planner deliberately leaves some 1475-rated slots unused to keep
  narrow providers at one call; the expected quality is ~98.3% of the
  per-request best (quality column 98), the same column the greedy
  leaders score, while the expected upstream-call count is ~22.5
  instead of ~24+ for a flat cap-2 greedy plan.
