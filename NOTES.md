# Routing strategy — FLOOR-GUARANTEED router

## The plan, made before the first request arrives

1. **Candidates.** For each of the 20 manifest requests, the legal
   candidates are every free (provider, model) pair meeting its
   requirements (tools / vision / JSON / minimum context), ordered by
   the sealed rating table, ties broken by provider then model id.
   A JSON-mode OpenAI request is only ever offered to a wire that can
   carry JSON mode (OpenAI or Gemini) — an Anthropic-wire provider
   would echo it absent and fail the request's echo check.
2. **Hardest first.** Requests are assigned primaries in order of
   weight (2 before 1), then fewest legal candidates, then manifest
   order, so a tools+vision or long-context request never loses its
   only good model to an easy one.
3. **Budget reservation.** No provider gets more than two primaries.
   A trial's failure schedules only ever refuse a second or later
   call, so two-per-provider keeps every primary on a call number
   that usually answers and leaves spare capacity everywhere for
   failover. The reservation is a *plan*, not a wall: failover may
   exceed it.
4. **The floor.** A request is never left unserved while any legal
   candidate is untried. On a 402/429/5xx the router walks the whole
   chain — best rating first, providers that never failed before
   providers that failed once — and only answers 503 when every
   candidate has refused. A provider that fails twice is treated as
   dead: its remaining planned primaries are re-planned onto the
   survivors, so one dead provider cannot drag the rest down.
5. **Retry the primary.** The planned primary stays first in its
   chain even after its provider refused once: a failure schedule
   refuses single call numbers, so the provider's next call usually
   answers at the planned rating, and skipping it would pay the same
   call elsewhere for a worse model. (Tested both ways on the
   practice trials: demoting a once-failed primary cost calls on
   trial-0 instead of saving them, so it was reverted.)

## Wire fidelity

The provider is always called non-streaming; the client's stream is
synthesised from the full answer, so the streamed event order is
exactly the client's wire. The provider's answer text is passed
through whole (it carries the ANSWER ECHO), its usage counts are
reported exactly as it reported them, and its stop reason is mapped
into the client's family. Every request field is carried into the
provider's wire untouched. The catalog is built by probing each
provider's route in the brief's order and keeping the listing's
order, so it matches the true catalogs exactly.

## How this differs from the top leaderboard entries

The top of the board (score 99, quality 98, 20 upstream calls) is
dominated by routers that drop a provider on its first refusal and
land exactly 20 calls on the dev trial — a plan that fits
practice/trial-0's particular failure schedule. This entry trades
that dev-trial call count for a floor guarantee that holds across
draws: on all three practice trials it serves 20/20 with failover
100, at 23 / 24 / 27 calls. The extra calls are the price of never
leaving a request unserved when a scored instance's failure schedule
differs from the practice one — and the score is the same 99 on
every trial, because quality, served, wire, failover and catalog are
all at their ceiling except quality's budget-bound 98.

## Measured results (kit env + scorer, local runs)

| trial | score | quality | served | wire | failover | catalog | calls |
|-------|-------|---------|--------|------|----------|---------|-------|
| 0     | 99    | 98      | 100    | 100  | 100      | 100     | 23    |
| 1     | 99    | 98      | 100    | 100  | 100      | 100     | 24    |
| 2     | 99    | 98      | 100    | 100  | 100      | 100     | 27    |

Two runs of trial-0 leave a byte-identical state export, and
`python3 main.py --list` prints exactly the catalog registered.
