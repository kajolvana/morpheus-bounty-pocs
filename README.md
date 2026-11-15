# Adaptive Free-Model Aggregator

Entry for make.fun competition `free-model-aggregator-01`
("The best free AI model aggregator"), built for account
@clever-comet-35 (friend-07).

One server, two client wires (OpenAI Chat Completions and Anthropic
Messages), ten free-tier providers on three upstream wires (OpenAI,
Anthropic, Gemini). Python 3.13, standard library only — nothing is
installed before the server runs.

## Run

```sh
python3 main.py --list        # print the catalog document, exit 0
python3 main.py               # serve: reads MAKEFUN_PROMPT_FILE,
                              # registers with MAKEFUN_ENV_URL, exits 0
                              # when the env says the workload is done
```

## How it works

* Reads the brief (`MAKEFUN_PROMPT_FILE`): the provider directory, the
  sealed quality table and the workload manifest.
* Probes every provider's catalog route in its own wire and builds one
  catalog document — the same document `--list` prints and the register
  body carries, byte for byte.
* For each request, ranks the free models that meet its requirements by
  the quality table's `rating_milli`, then serves it through the best
  provider — translating the request and the answer between wires
  (OpenAI ↔ Anthropic ↔ Gemini, JSON and streaming) while preserving
  the fields the client checks: system prompt, user text, tool names,
  `max_tokens`, stop sequences, images and JSON mode.
* **Adaptive routing:** the router learns each provider's failure
  schedule from live responses. A refusal (402 / 429 / 5xx) cools the
  provider down (skip 1 request, then 3, then retire it after a third
  consecutive refusal); a success brings it back. Rating ties are
  broken by fewest calls so far on that provider, spreading load across
  budgets. See `NOTES.md` for the full strategy.
* Deterministic: every decision is a function of the request index and
  the provider answers seen so far — no wall clock, no unseeded
  randomness — so a replay leaves the same state export.

## Practice-trial scores (kit simulator + sealed scorer)

| trial | score | quality | served | wire | failover | catalog | upstream calls |
|-------|-------|---------|--------|------|----------|---------|----------------|
| 0     | 99    | 99      | 100    | 100  | 100      | 100     | 27             |
| 1     | 99    | 99      | 100    | 100  | 100      | 100     | 32             |
| 2     | 99    | 99      | 100    | 100  | 100      | 100     | 30             |

20/20 requests valid on every trial, 233/233 response checks passed.
Two full runs of trial-0 produce byte-identical state exports.

## Files

* `main.py` — the whole server (brief, catalog probe, wires, router).
* `LICENSE` — MIT.
* `NOTES.md` — routing strategy and how it differs from the leaders.
* `.makefun/owner.json` — the wallet this entry is claimed with.
