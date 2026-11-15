# Routing strategy — failover first

## The problem as the trial poses it

Twenty requests, one at a time. Ten providers, each with a hidden
per-trial budget, a hidden schedule of refused calls, and (for one of
them) an outage part-way through. A refusal costs a call and, if the
request is then dropped, costs served/failover points too. The
quality table is public, so the *ceiling* per request is known; the
budgets and failure schedules are not, so no plan can promise to
dodge every refusal. The only honest lever is what happens *after*
the first refusal.

## The ladder

For each request, in manifest order, build the ladder from the
brief's quality table: every free model meeting the request's
requirements (tools, vision, JSON mode, minimum context), sorted by
rating descending, ties broken by provider id then model id so the
order is identical on every run. Walk it top to bottom; the first
provider that answers ends the request. A request is never answered
by a paid model, and never by a model that misses a requirement.

Two wire rules the ladder enforces, learned from the providers'
echoes: an OpenAI client asking for JSON mode is never sent to an
Anthropic-wire provider (that wire cannot echo JSON mode back), and
every upstream call carries the client's system prompt, tools, stop
sequences, `max_tokens`, images and JSON mode faithfully translated
into the provider's own wire — the provider echoes what it parsed,
and the client compares that echo with what it sent.

## Failover: believe the first refusal

This is where this entry spends its effort, and where it differs
from the leaderboard's top entries.

The top of the leaderboard (score 99, quality 98, 20 upstream
calls) is dominated by quality-first routers that hit the 20-call
floor: they spread the 20 requests across providers so thinly that
no provider ever reaches a scheduled refusal. That is the best
possible tie-break, but it is a *plan* made before the trial starts:
it pays for the floor with quality (a request goes to a weaker
model on an untouched provider rather than the best model on a busy
one), and it has no answer for a trial whose failure schedule lands
earlier than the plan assumes — a provider that fails on its second
call breaks the plan on the second request it serves.

This entry does the opposite. It always tries the best model first
and treats the first refusal as information:

- **Refusal without `retry-after` → provider dead, immediately,
  for the rest of the trial.** Budget spent, scheduled failure and
  outage are indistinguishable from the outside, and re-probing a
  dead provider only burns calls and risks the request. The ladder
  moves on to the next rung at once.
- **Refusal with `retry-after` → wait exactly that long, retry the
  same provider once.** A 429 with a retry-after is rate-limiting,
  not exhaustion; the next call ordinal usually answers, so the top
  model stays available. A second refusal kills the provider too.
- **400 → that model only.** A bad model or bad key does not condemn
  the provider's other models.
- **One success ends the request.** Exactly one provider call ever
  answers a request, as the scorer requires.

The cost is a few extra upstream calls versus the 20-call floor
(roughly one per provider that refuses once). The payoff is that
quality never pays for the plan: requests keep getting the best
model that is still alive, and a trial with an early or unlucky
failure schedule still serves 20/20 instead of dropping requests
when its pre-computed spread runs out.

## Wire fidelity

Upstream calls are always plain JSON, even for streamed client
requests. The provider's answer is parsed once into text, usage and
stop family, then re-framed: OpenAI JSON, OpenAI SSE (role chunk,
content deltas, finish chunk, usage chunk, `[DONE]`), Anthropic JSON
or Anthropic SSE (`message_start` → deltas → `message_delta` →
`message_stop`). The provider's text is carried through whole, its
usage counts are reported exactly as that wire reports them, and its
stop reason maps to the client's wire (`stop` / `end_turn`). The
server's own `GET /v1/models` answers in the shape of the auth
header it got and lists exactly the free model ids registered.

## Determinism

No wall clock and no unseeded randomness in any routing decision.
The ladder order, the dead-provider set and the retry waits are all
functions of the brief, the requests and the providers' answers.
`--list` prints the same catalog document the server registers,
byte for byte. The only sleeps are the providers' own retry-after
values and the poll of `/work`, neither of which is written into
anything sent.
