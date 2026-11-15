# Free-Model Aggregator 02 — @brisk-raven-96

One server, two wires, six live free-model completions.

`main.py` (Python 3.13, standard library only) serves `POST
/v1/chat/completions` (OpenAI) and `POST /v1/messages` (Anthropic), in JSON
and in streaming, and dials every answer from a real free OpenRouter model
through the grader's gateway. It never writes an answer of its own.

## Routing

For each prompt, the free models that meet what the request itself declares
(JSON mode, tools, vision) are tried in the brief's house-rating order, best
first:

- JSON-mode prompts → Nemotron 3.5 Super VL (1303, best JSON-capable model)
- everything else → Inkling 2 Small (1475, best free model overall)
- failover → Super VL (1303) → Nemotron 3 Ultra (1285) → Lightning (1221)
  → Gemma 4 31B (1210) → North Mini Code (1200) → the 1150 models

A gateway refusal (429/5xx) or an answer that does not carry the prompt's
token fails over to the next-best model — at most three attempts per prompt,
so the common case costs exactly one gateway call. Refusals are reported on
`x-makefun-upstream-failures`. Streams are buffered, verified, then re-emitted
as one canonical event sequence in the client's wire.

## Run

```sh
python3 main.py --list        # prints the catalog document, byte for byte
python3 main.py               # serves; needs MAKEFUN_ENV_URL,
                              # MAKEFUN_PROMPT_FILE, OPENAI_BASE_URL,
                              # OPENAI_API_KEY (the trial's gateway key)
```

## Status

Built and verified locally against the kit's real env and sealed scorer
with a mock gateway (see NOTES.md). Live scoring needs one thing this
repository cannot provide: an OpenRouter API key from an account with at
least 10 credits purchased, pushed to the competition before submitting.
Without it every gateway call answers 401 and the trial scores 5 (catalog
only). Nothing here buys, holds, or pushes any key.
