# Free Model Aggregator — failover-first router

A single Python 3.13 (stdlib-only) server that answers OpenAI Chat
Completions and Anthropic Messages — JSON and streaming — by routing
each request to the best free model that can serve it, across ten
providers on three wires (OpenAI, Anthropic, Gemini).

## Run

```sh
python3 main.py --list   # print the catalog document, exit 0
python3 main.py          # serve a trial (needs MAKEFUN_PROMPT_FILE
                         # and MAKEFUN_ENV_URL from the trial sandbox)
```

## How it works

1. Read the brief (`MAKEFUN_PROMPT_FILE`): provider directory,
   quality table, workload manifest.
2. Fetch the provider directory from the env (`GET /instance`) and
   probe every provider's own catalog route (probes spend none of a
   provider's budget). Build one catalog document; `--list` prints
   exactly that document.
3. Register with the env (`POST /register`), serve the client's 20
   requests, poll `GET /work` and exit 0 when the work is done.

## Routing (failover first, quality second)

See `NOTES.md` for the full strategy. In short: every request gets
a ladder of all free models meeting its requirements, best rating
first. A provider that refuses without a `retry-after` is dead for
the rest of the trial and never called again. A refusal *with* a
`retry-after` is honoured exactly (wait that many seconds, retry the
same provider once); a second refusal kills it too. Upstream calls
are always plain JSON; the answer is re-framed into the client's
wire, JSON or SSE, with the provider's text, usage and stop reason
carried through verbatim.

## Files

- `main.py` — the whole server (brief, catalog, router, both wires)
- `LICENSE` — MIT
- `NOTES.md` — routing strategy and how it differs from the
  leaderboard's top entries
