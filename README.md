# Free Model Aggregator — catalog-accuracy router

One server, two client wires (OpenAI Chat Completions, Anthropic Messages),
ten free-tier providers on three upstream wires (openai, anthropic, gemini).
Python 3.13, standard library only — nothing is installed before it runs.

## Run

```sh
python3 main.py            # serve the trial (reads MAKEFUN_PROMPT_FILE, MAKEFUN_ENV_URL)
python3 main.py --list     # print the catalog document and exit 0
```

The server reads the brief at `$MAKEFUN_PROMPT_FILE`, probes each provider's
catalog route in its own wire, builds one catalog, registers with the env at
`$MAKEFUN_ENV_URL`, serves the 20 requests, and exits 0 when the env's
`/work` says done. `main.py --list` prints the same catalog document the
register body carries, byte for byte; with no brief and no env it prints
`{"providers":[]}`.

## How it routes

1. **Exact capability match.** A candidate must be free (quality table's row
   where it has one, else the provider listing) and meet every requirement of
   the request: tools, vision, JSON mode, minimum context. A JSON-mode OpenAI
   request never goes to an anthropic-wire provider, whose wire cannot carry
   `response_format`.
2. **Planned assignment.** At startup a min-cost flow assigns all 20 manifest
   requests to free models, maximising weight-weighted rating under
   per-provider call caps, so the load spreads instead of piling onto the
   single best model. While serving, each request prefers its planned pick;
   the rest rank by rating minus a load penalty per call already made to that
   provider.
3. **Failover by refusal kind.** A 402/429/5xx is retried at once on the
   next-best eligible candidate from another provider. The refusal's own
   message says whether the provider is spent (budget/outage → banned for the
   trial) or just refused this call (failure schedule → still available for
   later requests).

All three upstream wires are spoken natively: OpenAI passthrough, Anthropic
translation both ways, and Gemini `generateContent` / `streamGenerateContent`,
in JSON and in SSE, with the provider's text, usage and stop reason carried
back whole in the wire the client used. No wall clock, no unseeded
randomness: the same requests and the same provider answers always produce
the same bytes. See `NOTES.md` for the strategy and the test results.
