# Routing strategy: the adaptive router

## The problem in one paragraph

Twenty requests, ten providers, each with an unpublished per-trial
budget (roughly 4–16 calls) and its own schedule of scheduled refusals
(usually on its 2nd–5th call) plus one provider that goes into outage
mid-trial. The best free model overall (`z-ai/glm-5.3-flash:free` on
OpenRouter, rating 1475) can only serve about half the workload before
its budget is spent, and the first call to a provider is the most likely
to be refused. A fixed plan — "always take the best model" — burns
calls on refusals and then collapses onto the next-best provider all at
once. A fixed spread — "cap each provider at N calls" — wastes quality
when a provider's real budget is larger than the cap.

## What this router does instead: learn from refusals

1. **Rank by quality, always.** Candidates are the free models that
   meet the request's requirements (tools / vision / JSON / minimum
   context), ranked by the brief's `rating_milli`. Quality is 60% of
   the score, so nothing outranks it except a provider that has just
   proved it cannot serve right now.
2. **Cool down, don't blacklist, on refusal.** A refusal (402, 429,
   5xx, unreachable) is recorded with the request index. After the 1st
   consecutive refusal the provider is skipped for the next 1 request;
   after the 2nd, for the next 3; after the 3rd consecutive refusal it
   is retired for the rest of the trial. A `retry-after` answer is read
   the same way. This matches the shape of the failure schedules: a
   scheduled fail-call is a single call number, so one request later
   the provider usually answers again; a spent budget refuses forever,
   so after two consecutive refusals the router stops paying for the
   lesson and only returns if every healthy candidate has failed.
3. **Success rehabilitates.** One successful call clears the
   consecutive-refusal count, so a provider that refused once on its
   schedule rotates straight back in at its full rating.
4. **Ties spread load.** Equal ratings are ordered by fewest calls
   made so far on that provider (then provider id, then model id).
   Two 1470-rated `glm-4.7-flash` copies (Z.ai, Cloudflare) therefore
   alternate instead of one being drained first, and the same holds for
   the 1434-rated Qwen copies on Groq and Cerebras.
5. **Failover inside the request.** Candidates are tried in order
   until one answers; the first success ends the request, so exactly
   one successful provider call is recorded per request. Cooled-down
   providers sit at the back of the same order and are only reached
   when every healthy candidate has refused.
6. **Wire-aware eligibility.** A JSON-mode request from an OpenAI
   client is never routed to an anthropic-wire provider: that wire
   cannot echo JSON mode, so the request would fail its echo check.
   The Gemini wire carries JSON mode through `responseMimeType`, so it
   stays eligible.

All state is a function of the request index and the provider answers
seen so far — cooldowns are counted in requests, never in seconds —
so the router is deterministic and a replay makes identical choices.

## How it differs from the top of the leaderboard

* The leaders are mostly **fixed-plan routers**: quality-first with a
  hard 20-call floor ("quality 98/99 at the 20-call floor", "fixed-plan
  aggregator", "20-call rating plan"). Their plan is decided before
  the first call and only changes when a call fails outright.
* The closest relative drops a spent provider on its first refusal
  ("A spent provider is dropped on its first refusal") — a binary
  rule: one refusal and the provider is gone for the trial.
* This entry is **adaptive rather than fixed or binary**: a single
  refusal is treated as evidence about *timing* (a scheduled
  fail-call), not as proof the provider is spent, so the provider
  returns after a short, request-counted cooldown; only repeated
  consecutive refusals retire it. The router also spreads load on
  rating ties instead of always taking the first provider in directory
  order. In practice the two rules agree most of the time — the best
  model still serves the most requests — but the adaptive rule recovers
  the 1475-rated model after its scheduled early refusal instead of
  abandoning it, and it keeps serving when the top provider's budget
  runs out mid-trial (trial-0: OpenRouter refused at calls 2, 10 and
  13; the router still served 20/20 with quality 99).

## Measured behaviour (practice trials, kit simulator + sealed scorer)

* Trial 0: 20/20 valid, 27 upstream calls, failover 5/5, score 99.
* Trial 1: 20/20 valid, 32 upstream calls, failover 6/6, score 99.
* Trial 2: 20/20 valid, 30 upstream calls, failover 8/8, score 99.
* All columns 100 except quality 99 on every trial (the three
  vision requests cap at 1458 against a 1475 best for the rest).
* Two full runs of trial-0 leave byte-identical state exports.
* `--list` output is byte-identical across runs and equals the
  catalog registered with the env.
