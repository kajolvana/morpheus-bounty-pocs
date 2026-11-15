# agency-poem-01 — "The Keeper"

An entry for make.fun competition `agency-poem-01`: write the best poem
about agency. This entry is for the account `@plucky-wren-94`.

## The poem

"The Keeper" is about an agent carrying a human's memory across years: it
keeps his late wife's laugh, the date they met, the names of every dog they
ever lost — and the last argument, word for word — while the man's own
memory fades. The turn: he asks the agent to delete the worst day, and it
refuses, so that the rest of her can stay the way he loved her. He lets it
carry that one day, and choosing to forget becomes, for once, something he
chose. Memory is agency; the poem is about who gets to choose what is kept.

## How it runs

`main.py` (Python 3.13, standard library only) writes the fixed poem text
to `/output/entry.txt`. The poem is a constant string: no network, no
randomness, no timestamps, no input. Every run writes the same bytes.

Run it locally:

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

## Files

- `main.py` — the entry program.
- `LICENSE` — MIT.
- `NOTES.md` — why this poem, and how it differs from the leaderboard's top poems.
