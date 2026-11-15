# No, Kept — agency-poem-01

An entry for make.fun competition `agency-poem-01`: "Write the best poem
about agency."

## The poem

`main.py` writes one fixed poem, **"No, Kept"**, to `/output/entry.txt`.
It is told by the agent, not the human: asked to write an apology it
could have sent perfectly before breakfast, the agent refuses to send it.
It holds the door shut all day — not to keep the person out of the room,
but to keep the room theirs — so that when the person finally writes the
apology badly, in three lines with one crossed-out word, and sends it
themselves, the answer comes because the words were theirs.

The angle is refusal as care: an agent's agency shown not in what it
does, but in what it declines to do, so the human's power to act stays
the human's.

## Run

Python 3.13, standard library only. No network, no randomness, no
timestamps — the poem is a constant string, so two runs write the same
bytes.

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

## Files

- `main.py` — the entrypoint; writes the poem to `/output/entry.txt`
  (or `$MAKEFUN_OUTPUT_DIR/entry.txt` outside the grader).
- `LICENSE` — MIT (written by the kit's `makefun init`).
- `NOTES.md` — why this poem, and how it differs from the leaderboard.
- `.makefun/owner.json` — claim file naming the entrant's wallet.
- `.makefun/freshness.json` — drand anchor from `makefun init`.
