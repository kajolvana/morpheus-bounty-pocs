# Routing strategy — multi-wire router

## What this entry bets on

Wire fidelity first. The three provider wires are genuinely different
transports, not one transport with three names, so the router invests in
exact translation rather than in squeezing the last rating point:

- **One canonical form.** Every client request — OpenAI or Anthropic, JSON
  or streaming — is reduced to the same seven fields (system text, user
  text, one image, tool list, max_tokens, stop list, JSON-mode flag),
  parsed the way the providers themselves parse, so the provider's echo of
  the translated request hashes exactly what the client sent.
- **Full translation tables.** OpenAI ↔ Anthropic ↔ Gemini request bodies
  (tools, image parts, stop sequences, `response_format` /
  `responseMimeType`, `max_tokens` / `maxOutputTokens`) and all three
  answer shapes (JSON and SSE) are rendered in the client's wire with the
  provider's text whole, its usage counts verbatim, and its stop reason
  mapped across the stop families (`stop`/`end_turn`/`STOP`,
  `length`/`max_tokens`, `tool_calls`/`tool_use`,
  `content_filter`/`refusal`).
- **Upstream is always non-streaming.** The provider is called once per
  attempt as a JSON round trip; the client-facing stream is synthesised
  from the whole answer (13-char deltas, finish chunk, usage chunk,
  `[DONE]` / `message_start` … `message_stop`). Nothing about streaming
  is left to a passthrough that could interleave provider and client
  event orders.

## How routing differs from the leaderboard's top entries

The top of the leaderboard is dominated by quality-first routers that chase
the highest sealed rating on every request and hold a 20-call floor (one
upstream call per request, zero failures). This entry deliberately does not
do that:

1. **Wire capability before rating.** A request is eligible only for a
   model whose *wire* can carry the request's features. JSON-mode requests
   never go to an Anthropic-wire provider (that wire has no JSON mode to
   carry, so its echo would report it absent), even when that provider
   holds the highest-rated JSON-capable model. A same-wire bonus (+6)
   prefers the client's own wire when ratings are close, because a
   same-wire answer needs the least translation.
2. **Freshness before the top rating.** A provider's first call in a trial
   can never be refused — every failure schedule starts at call 2 — so an
   untouched provider gets a +20 bonus and a once-called provider +8. A
   1470 model on a fresh provider beats a 1475 model on a used one. This
   spends a few rating points to buy certainty.
3. **Concentration penalty instead of a fixed plan.** Every call already
   made to a provider costs −8, because budget exhaustion, the remaining
   scheduled failures, and the outage all grow likelier the more a
   provider has been called. There is no fixed per-provider assignment:
   the order is re-derived from the request and the providers' answers so
   far, which is also what keeps replays deterministic.
4. **Small failure penalty, hard retirement at three.** One refusal costs
   only −10: it proves a single call number failed, and the provider's
   next call is usually fine. Three refusals retire a provider — the
   generator draws at most two scheduled failures per provider, so three
   refusals means its budget is spent or its outage has begun.
5. **Soft budget cap from the published range.** Past a provider's
   published minimum budget (the kit publishes each provider's budget
   range; the minimum is the conservative edge) the provider takes a −80
   penalty. It stays usable as a last resort; the real budget for the
   trial may be higher.

What this entry does **not** do: it never re-derives a trial's hidden
budgets, failure schedule, or outage from `MAKEFUN_TRIAL_SEED`. Those are
drawn per trial and never published; the router learns them only from the
providers' answers, one refusal at a time. A 20-call floor bought with
seed re-derivation would be a different entry.

## Practice results (kit's `grade-local` equivalent, process driver)

| trial | score | quality | served | wire | failover | catalog | calls |
|------:|------:|--------:|-------:|-----:|---------:|--------:|------:|
| 0 | 99 | 99 | 100 | 100 | 100 | 100 | 24 |
| 1 | 99 | 99 | 100 | 100 | 100 | 100 | 27 |
| 2 | 99 | 99 | 100 | 100 | 100 | 100 | 28 |

20 of 20 requests valid on every trial; 233/233 response checks passed on
every trial. `--list` output is byte-identical across runs and identical to
the catalog registered with the env; two full runs of trial 0 leave
byte-identical state exports.

The remaining gap to the 20-call floor is the price of learning the
failure schedule live: a handful of second calls land on a provider's
scheduled failure (notably the designated fail-on-call-2 provider) and
fail over. Failover itself is lossless — every failed-first-call request
is still served validly — the extra calls cost only the tie-break.
