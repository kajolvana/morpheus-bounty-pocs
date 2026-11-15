# Free Model Aggregator — Quality-99 Floor Router

One server (`main.py`, Python 3.13, standard library only) that serves
the OpenAI Chat Completions API and the Anthropic Messages API — JSON and
streaming — over ten simulated free-tier providers.

## Run

```sh
python3 main.py            # read $MAKEFUN_PROMPT_FILE, register at $MAKEFUN_ENV_URL, serve, exit 0 when done
python3 main.py --list     # print the catalog document and exit 0
```

## How it works

- The brief (`$MAKEFUN_PROMPT_FILE`) carries the provider directory, the
  quality table and the workload manifest. The catalog is built from the
  brief alone, so `--list` prints byte for byte the document the server
  registers.
- Each request's requirements come from its manifest row (requests arrive
  in manifest order) merged with signals read from the body itself: tools
  present, an image block, JSON mode (a `response_format` or a prompt
  asking for one JSON object), and a long user text (long-context).
- Candidates are the free models meeting the requirements, ordered by
  **penalized rating**: `rating_milli - 8 * (calls already made to that
  provider)`. The first candidate that answers wins; a 402/429/5xx moves
  to the next candidate, so failover is automatic and exactly one provider
  call ever answers a request.
- Every request is translated into the chosen provider's own wire
  (OpenAI, Anthropic, or Gemini) with its system prompt, user text and
  images, tools, stop sequences, `max_tokens` and JSON mode carried over
  untouched, and the provider's answer is returned whole in the wire the
  client used, with the provider's usage counts and the mapped stop
  reason. An Anthropic-wire provider is never used for an OpenAI JSON-mode
  request, because its wire cannot echo JSON mode.
- No wall clock, no unseeded randomness: every choice is a function of
  the brief, the requests and the providers' answers, so a replay leaves
  the same state export.

## Practice results (kit simulator + scorer)

| trial | score | quality | served | wire | failover | catalog | calls |
|-------|-------|---------|--------|------|----------|---------|-------|
| 0     | 99    | 99      | 100    | 100  | 100      | 100     | 27    |
| 1     | 99    | 99      | 100    | 100  | 100      | 100     | 29    |
| 2     | 99    | 99      | 100    | 100  | 100      | 100     | 26    |

Two full runs of trial 0 leave byte-identical state exports.

See `NOTES.md` for the routing strategy and how it differs from the
other routers on the leaderboard.
