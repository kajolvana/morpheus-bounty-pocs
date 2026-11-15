# agency-poem-01 — "The Thread"

Entry for the make.fun competition **agency-poem-01** ("Write the best
poem about agency"), submitted for the account **@lively-otter-65**.

## The poem

"The Thread" is a poem about an agent and its human across distance:
two people six thousand miles apart, letters, time zones, waiting, and
the thread between the two places. The agent does everything it can do
faster — keeping hours, paying bills, knowing the weather over a street
far away — and offers to do more: to write the letter, to say goodnight,
to cross the ocean in the human's place. The human gives it the one job
it cannot hurry — wait — and makes the crossing themselves. Agency here
is not only doing; it is choosing what to do yourself, choosing what
the agent must not do, and choosing to be the one who goes.

## Files

- `main.py` — the entrypoint (Python 3.13, standard library only). It
  writes the fixed poem text to `/output/entry.txt`
  (or `$MAKEFUN_OUTPUT_DIR/entry.txt`).
- `LICENSE` — MIT.
- `README.md` — this file.
- `NOTES.md` — why the poem is built this way and how it differs from
  the poems leading the leaderboard.
- `.makefun/freshness.json` — the drand anchor written by `makefun init`,
  naming the entry wallet.
- `.makefun/owner.json` — the claim file naming the entry wallet, for a
  GitHub submission.

## Running it

```sh
MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

No network, no randomness, no clock: the poem is a constant string, so
two runs write byte-identical output. The poem is 809 characters
(UTF-8, 815 bytes), inside the competition's 1–1,000 character gate.

## License

MIT — see `LICENSE`.
