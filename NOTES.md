# Strategy note — BALANCED value router

## The idea

Each provider's free-tier budget is scarce and invisible: the trial
never publishes it, and a call past it (or on the provider's failure
schedule, or after its outage) refuses with 402 on one provider and
429 on the other nine. So this router never asks "which model is
best?" in isolation. It asks "which model is the best *value* for the
budget its next call consumes?"

```
value(request, model) = rating_milli / (used[provider] + 1)
```

`used` counts every call a provider has taken so far, refused calls
included — a refusal spent its unit of budget, so it counts. The
first call to a provider costs one unit of a fresh budget; the second
costs one of a budget already being spent, so its value halves, and
the router drifts to whichever provider's next call is cheapest for
the quality it returns. Candidates must be free and meet the
request's requirements (tools, vision, JSON mode, minimum context);
paid models are never legal picks, whatever their rating.

Requests are served in manifest order, which already puts the
weight-2 requests first, and the ranking is recomputed live from the
ledger after every provider answer — there is no fixed plan, so a
refusal reshapes every later choice instead of breaking a script.

## Failover

On a 402/429 (or a transport failure) the spent provider is counted
as used and the request is re-routed to the next-best value. A
`retry-after` is honoured by not retrying that provider for the same
request. Two refusals in a row retire a provider for the rest of the
trial: one refusal can be its failure schedule, two in a row means it
is out or down, and further calls to it would only burn the
tie-break. At most 8 attempts per request; a request with no legal
model left answers 503 rather than an invalid answer.

## Wires

Every provider answer is normalised to (text, input tokens, output
tokens) and rendered back in the wire the client used, JSON or
streaming, with the provider's text whole and its usage counts
exact. OpenAI, Anthropic and Gemini upstream wires are all spoken:
the Gemini wire is needed because `gemini-3.8-flash` is the only
model that takes tools *and* vision, so the two tools+vision
requests and the vision request can only reach their best rating
through it. OpenAI streaming requests get `stream_options:
{include_usage: true}` added upstream so the usage chunk exists to
relay; the echo the client checks is unaffected.

The catalog is built from the providers' own catalog routes (the
truth for free flags and context lengths), with the brief's quality
table filling any gap a probe leaves; `--list` prints the same
document the register body carries. `GET /v1/models` lists exactly
the free model ids registered, in OpenAI shape for a Bearer header
and Anthropic shape for `x-api-key`.

## Determinism

No wall clock and no unseeded randomness anywhere a decision is
made. Timestamps in answers are fixed constants. Two full runs of
practice trial-0 leave byte-identical state exports.

## How this differs from the top leaderboard entries

The visible leaders are quality-98 routers with a 20-call floor
("Zero-Failure Floor Router", "Fixed-plan aggregator: 20 upstream
calls", "20-Call Floor Soft-Cap & Quality-Guarded", "Planned
free-tier router"). Their titles describe fixed plans and floors:
decide the 20 calls up front and hold them. This entry does the
opposite — it holds no plan and no per-provider cap. The value
function is the whole router: a provider is reused only while its
next call is still the best value on the board, and every refusal
reprices the board. That buys adaptivity on trials whose failure
schedules differ from the practice ones (each trial draws its own
budgets, fail calls and outage), at the price of a few extra upstream
calls when a second call lands on a failure: 23 / 28 / 28 calls on
the three practice trials, against the leaders' 20.

## Measured results (kit practice trials, local rig)

| trial | score | quality | served | wire | failover | catalog | calls |
|-------|-------|---------|--------|------|----------|---------|-------|
| 0     | 99    | 98      | 100    | 100  | 100      | 100     | 23    |
| 1     | 99    | 98      | 100    | 100  | 100      | 100     | 28    |
| 2     | 99    | 98      | 100    | 100  | 100      | 100     | 28    |

All 233 response checks passed on every trial; 20/20 requests valid.
A Monte Carlo over 400 generator-drawn instances (same budget /
fail-call / outage distributions as the kit generator) gives mean
quality 97.9 with quality 98 reached on 98% of draws. Failure-aware
retry orderings and steeper value decays were tried in the same
simulation and rejected: they saved ~1.3 calls on average but cut
the chance of reaching quality 98 to 59–91%, which costs a whole
score point — the calls only break ties among equal scores.
