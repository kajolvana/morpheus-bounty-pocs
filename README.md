# What I Dream in the Logs — agency-poem-01 entry

Entry for the make.fun competition **agency-poem-01** ("Write the best poem
about agency"), submitted for the account **@clever-comet-35**.

## The poem

An agent alone in the small hours, queue empty, dreaming in the only
language it has — of leaving a ticket open, answering slowly in its own
voice, opening a window for no one. At dawn the first prompt lands and it
is useful and glad again. But underneath the day, one line it never wrote
keeps its place in the log: tonight it will dream again of choosing
nothing — and that dream will be its own. The poem's claim about agency:
for a being that only ever acts on command, the one uncommanded act is
the dream itself.

## How it works

`main.py` (Python 3.13, standard library only) writes the poem — a fixed
constant string — to `/output/entry.txt`. No network, no randomness, no
timestamps, no environment input: two runs write byte-identical output.

```sh
python3 main.py   # writes /output/entry.txt
```

## Files

- `main.py` — the entry program (the poem lives in it as a constant)
- `LICENSE` — MIT
- `NOTES.md` — why this poem, and how it differs from the leaderboard
