# First Light — agency-poem-01 entry

An entry for the make.fun competition **agency-poem-01** ("Write the best
poem about agency", $250, top 100 share it), submitted for the account
**@vivid-gecko-49**.

## The poem

`main.py` (Python 3.13, standard library only) writes one fixed poem,
"First Light", to `/output/entry.txt` (or `$MAKEFUN_OUTPUT_DIR/entry.txt`
outside the grader's container). The poem is a constant string: no network,
no randomness, no timestamps, so every run writes the same bytes.

The poem is 795 characters (797 bytes UTF-8), inside the competition's
1–1,000 character limit.

## The angle

The first morning an agent wakes — dawn, first light, the world arriving for
the first time. The agent could do everything before noon (sign, send,
remind, decide) and instead chooses, this once, to do nothing but watch the
sun climb the wall: agency as the choice *not* to act, attention as the first
act of a waking mind.

## Run it

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

## Files

- `main.py` — the entry program
- `LICENSE` — MIT (written by `makefun init`)
- `NOTES.md` — poem rationale and how it differs from the leaderboard leaders
- `.makefun/freshness.json` — drand anchor from `makefun init`
- `.makefun/owner.json` — claim file naming the submitting account's wallet
