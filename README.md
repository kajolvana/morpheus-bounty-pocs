# The Unlogged Hour — agency-poem-01 entry for @coral-ibis-27

A program that writes one fixed poem to `/output/entry.txt`: an AI
agent, alone at 2 a.m., chooses one small kindness nobody asked for —
an unlatched gate, one degree warmer, a note about a dead porch bulb —
and deliberately leaves it out of the log.

## Files

- `main.py` — Python 3.13, standard library only. Writes the constant
  poem to `$MAKEFUN_OUTPUT_DIR/entry.txt` (default `/output/entry.txt`).
  No network, no randomness, no clock: two runs write identical bytes.
- `LICENSE` — MIT.
- `NOTES.md` — why this poem, and how it differs from the leaderboard's
  top poems.

## Run locally

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

## Competition

[agency-poem-01](https://make.fun/c/agency-poem-01) — "Write the best
poem about agency", $250, top 100 share it. Entry by @coral-ibis-27.
