# Floor-Guaranteed Free-Model Aggregator

One server that serves the OpenAI Chat Completions API and the Anthropic
Messages API (JSON and streaming) over ten simulated free-tier providers,
routing each request to the best free model that can serve it and failing
over when a provider refuses — so all 20 requests get served even if it
means using a lower-quality model for some.

## Run

```sh
python3 main.py            # serve: reads MAKEFUN_PROMPT_FILE, registers
                           # with MAKEFUN_ENV_URL, exits 0 when /work is done
python3 main.py --list     # print the catalog document and exit 0
```

Python 3.13, standard library only. Nothing is installed.

## How it works

See `NOTES.md` for the routing strategy. In short: the server reads the
brief (provider directory, quality table, workload manifest), probes each
provider's catalog route, plans all 20 calls up front (hardest requests
first, at most two primaries per provider), then serves each request by
walking its ordered failover chain until a provider answers. A provider
that fails twice is treated as dead and its remaining planned primaries
are re-planned onto the survivors.

## Local verification (practice trials)

Against the kit's sealed env + scorer, all three practice trials score
99 (quality 98, served 100, wire 100, failover 100, catalog 100), 20/20
valid, 23 / 24 / 27 upstream calls. Two runs of the same trial leave a
byte-identical state export, and `python3 main.py --list` prints exactly
the catalog the server registers.

## License

MIT — see `LICENSE`.
