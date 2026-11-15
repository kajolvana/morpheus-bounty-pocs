# NOTES — routing strategy

## The idea in one line

For every request, spend the call on the best free model that can legally
serve it; when that provider refuses, spend the next call on the next-best
model, never on the same provider twice for one request.

## Why this is not what the leaderboard top does

The top of the board (quality 98, 20 upstream calls) is dominated by
**planned spread routers**: they pre-assign requests across providers so no
provider is ever called past its minimum budget and no call ever lands on a
failure-schedule slot — 20 calls, zero refusals, quality 98. That plan is
brittle: it bets the whole trial on budgets and failure slots it cannot see,
and any single refusal breaks the plan's accounting.

This entry is **quality-first with a second chance**:

1. **Rank by rating, always.** Candidates are the free models meeting the
   request's requirements, ordered by sealed rating descending (ties: fewer
   refusals, then provider, then model id). The request's own body is read
   for implied requirements (tools present, image present, JSON mode), and
   the workload manifest's requirements are unioned in by arrival order.
2. **JSON mode is a wire constraint, not just a model capability.** A
   JSON-mode request only goes to an OpenAI- or Gemini-wire provider: the
   Anthropic wire cannot echo `json_mode`, so routing there would fail the
   echo check even with a JSON-capable model.
3. **A refusal demotes, it does not kill.** Refusals sort after rating, so
   a once-refused provider is still tried before any lower-rated fresh one.
   Most refusals are one-off failure-schedule hits (every provider in the
   practice trials refuses 1–2 numbered calls), not a spent budget — the
   second chance recovers the best model on the very next request. A
   provider that refuses twice is retired for the trial, which bounds the
   waste from a genuinely spent budget to one extra call.
4. **Never retry the same provider on the same request.** That honours any
   `retry-after` without sleeping, and keeps exactly one successful
   upstream call per request (the `one_provider` check).
5. **Wire fidelity is load-bearing, not cosmetic.** The provider's answer
   text is an echo of the request it parsed, so the request crosses wires
   byte-faithfully (system text, user text, image bytes, tool names in
   order, stops, `max_tokens`, JSON mode) and the answer returns in the
   client's wire with the provider's exact text, usage counts and stop
   family. Streaming is buffered and re-emitted in the client's event
   order with usage included, because the usage check needs it.

## Measured results (kit practice instances, sealed env + scorer)

- trial-0/1/2: **score 99, quality 99, served 100, wire 100, failover
  100, catalog 100**, 20/20 valid, 29/29/30 upstream calls.
- `--list` output byte-identical to the registered catalog on all three
  trials (the catalog is built purely from the brief, never from a probe,
  so it cannot drift with env reachability).
- Two runs of trial-0 leave byte-identical state exports (deterministic).

The trade against the 20-call planners is deliberate: ~9 extra calls buy
quality 99 instead of 98, and every one of those calls is a recovered
refusal — failover 100 on all three trials.
