# Free-model aggregator — deterministic planner router

One server that serves the OpenAI Chat Completions API and the Anthropic
Messages API over ten simulated free-tier providers (OpenAI, Anthropic and
Gemini wires), in JSON and in streaming.

## Run

```sh
python3 main.py            # serve: reads $MAKEFUN_PROMPT_FILE, registers
                           # with $MAKEFUN_ENV_URL, exits 0 when done
python3 main.py --list     # print the catalog document and exit 0
```

Python 3.13, standard library only. No dependencies, no wall clock, no
unseeded randomness: the same brief and the same provider answers always
produce the same plan, the same answers and the same state export.

## How it routes

See NOTES.md. In short: the whole 20-request workload is solved up front
as one exact min-cost max-flow assignment (maximise total quality subject
to per-provider planned-call caps and capability constraints), then
executed with failover re-planning — a refused provider is excluded for
that request and the remaining workload is re-solved. Caps are set from
the kit's public generator ranges so that narrow-budget providers never
receive a planned second call (their second call fails about half the
time), while wide-budget providers take three.
