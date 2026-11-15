# Free Model Aggregator — Balanced Value Router

One server, two client wires (OpenAI Chat Completions and Anthropic
Messages, JSON and streaming), ten simulated free-tier providers.

`main.py` (Python 3.13, standard library only) reads the brief at
`$MAKEFUN_PROMPT_FILE`, probes each provider's catalog route, registers
its port and catalog with the env at `$MAKEFUN_ENV_URL`, answers the
20 requests the env's client sends, and exits 0 when the env says the
workload is done. `python3 main.py --list` prints the catalog document
it registers, byte for byte.

## Routing

BALANCED: every (request, model) pair is scored by quality per unit of
scarce budget — the model's rating divided by the budget units its
provider has already spent plus one — and the best-value legal model
serves the request. See `NOTES.md` for the full strategy.

## Run

```sh
python3 main.py --list   # print the catalog and exit 0
python3 main.py          # serve a trial (needs MAKEFUN_ENV_URL and
                         # MAKEFUN_PROMPT_FILE)
```

## Local verification (kit practice trials)

Score 99 on all three practice trials (quality 98, served 100,
wire 100, failover 100, catalog 100), 23 / 28 / 28 upstream calls;
two full runs of trial-0 leave byte-identical state exports, and
`--list` output matches the registered catalog exactly.
