# Free Model Aggregator — multi-wire router

One server, two client APIs (OpenAI Chat Completions and Anthropic Messages,
each in JSON and streaming SSE), ten simulated free-tier providers on three
wires (OpenAI, Anthropic, Gemini). Python 3.13, standard library only.

## Run

```sh
python3 main.py --list   # print the catalog document and exit 0
python3 main.py          # register with $MAKEFUN_ENV_URL, serve, exit 0 when done
```

The server reads the brief at `$MAKEFUN_PROMPT_FILE`, probes each provider's
catalog route to build one catalog, registers `{protocol_version: 1, port,
catalog}` with the env, serves the client's 20 requests one at a time, polls
`GET /work`, and exits 0 when the workload is done.

## Design

Every client request is reduced to one canonical form (system text, user
text, one image, tool list, max_tokens, stop list, JSON-mode flag), then
re-expressed exactly in the chosen provider's own wire, so the provider
echoes back precisely what the client sent. The provider is always called
non-streaming; the answer is re-rendered in the client's wire with the
provider's text whole, its usage counts verbatim, and its stop reason mapped
into the client's stop family.

Routing is wire-capability first, then quality: a request only goes to a
free model that meets its requirements and whose wire can carry its features
(JSON mode rides the OpenAI or Gemini wire only). Among the eligible,
candidates rank by sealed rating with a same-wire bonus, a fresh-provider
bonus (a provider's first call can never be refused), a growing
concentration penalty, a small failure penalty, and a heavy penalty past the
provider's published minimum budget. On a 402/429/5xx the request fails over
to the next candidate; after three refusals a provider is retired.

See NOTES.md for the strategy and how it differs from the leaderboard's
top routers.
