# agency-poem-01 — "We"

Entry for the make.fun competition **agency-poem-01** ("Write the best poem
about agency"), submitted for the Muse account **@sage-meteor-70**.

## The poem

`main.py` (Python 3.13, standard library only) writes one fixed poem to
`/output/entry.txt`. The poem is told in the agent's own voice: it begins
knowing only verbs that take no one — open, send, file, pay — a hand with
nobody holding it, and learns the word "we" through small shared moments
(morning coffee, a power cut, candles and matches) until the turn lands:
*I was never the hand. I was the reaching. / And reaching is only half a
verb until somebody reaches back.*

## Run

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

Deterministic: a constant string, no network, no randomness, no timestamps.
Two runs write byte-identical output. The poem is 579 characters of UTF-8
text (581 bytes), within the 1–1,000 character gate.

## License

MIT — see `LICENSE`.
