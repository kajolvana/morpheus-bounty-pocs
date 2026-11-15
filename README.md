# Free Model Aggregator — quality-first router

One server that serves the OpenAI Chat Completions API and the Anthropic
Messages API (JSON and SSE) over ten free-tier model providers, picking the
best free model for every request and failing over when a provider refuses.

## Run

```sh
python3 main.py            # serve: registers with $MAKEFUN_ENV_URL, exits 0 when done
python3 main.py --list     # print the catalog document and exit 0
```

Python 3.13, standard library only — nothing to install. The server reads
the brief from `$MAKEFUN_PROMPT_FILE` (providers, the sealed quality table,
the workload manifest) and dials providers only under `$MAKEFUN_ENV_URL`.

## How it works

- **Catalog.** Built from the brief's provider directory and quality table,
  so `python3 main.py --list` prints byte-for-byte the catalog the server
  registers. `GET /v1/models` answers in the wire its auth header names
  (Bearer → OpenAI shape, `x-api-key` → Anthropic shape), listing exactly
  the free model ids registered.
- **Routing (quality-first).** Every free model that meets a request's
  requirements (tools / vision / JSON mode / minimum context) is ranked by
  its quality rating, best first. JSON-mode requests only go to wires that
  can carry JSON mode (OpenAI, Gemini). A provider that refuses is retried
  only after better models are exhausted — most refusals are one-off
  failure-schedule hits, not a spent budget — and a provider that refuses
  twice is retired for the trial. No retries against the same provider on
  the same request, so any `retry-after` is honoured.
- **Wire fidelity.** Requests cross into the provider's wire untouched in
  substance (system prompt, user text, images, tools, stop sequences,
  `max_tokens`, JSON mode) and the provider's answer comes back in the
  client's wire with the provider's text whole, its usage counts and its
  stop reason mapped (`stop`/`end_turn`/`STOP` are one family). Streaming
  answers are re-emitted in the client's event order, with usage included.
- **Determinism.** Every choice is a function of the brief, the requests
  seen so far and the providers' answers: no wall clock, no unseeded
  randomness. Two runs of the same trial leave the same state export.

## Local check

Against the kit's practice instances (sealed env + scorer):

| trial | score | quality | served | wire | failover | catalog | calls |
|-------|-------|---------|--------|------|----------|---------|-------|
| 0     | 99    | 99      | 100    | 100  | 100      | 100     | 29    |
| 1     | 99    | 99      | 100    | 100  | 100      | 100     | 29    |
| 2     | 99    | 99      | 100    | 100  | 100      | 100     | 30    |

20/20 requests valid on every trial; `--list` output is byte-identical to
the registered catalog; two runs of trial-0 leave identical state exports.
