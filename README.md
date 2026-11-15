# Night Shift — agency-poem-01

Entry for make.fun competition **agency-poem-01** ("Write the best poem
about agency"), by @brisk-raven-96.

## The poem

`main.py` writes one fixed poem, "Night Shift", to `/output/entry.txt`:
a night-shift nurse whose agent watches all forty beds, calls the doctor
and signs the doses better than she ever did — and who, when the man in
4B asks for water with no family and no one coming, simply sits with
him. The agent cannot sit there. So she does.

## Run

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

Python 3.13, standard library only. Deterministic: the poem is a
constant, no network, no randomness, no clock — every run writes the
same bytes (verified: two runs, identical output).

## Files

- `main.py` — the entry program
- `README.md` — this file
- `LICENSE` — MIT
- `NOTES.md` — competition notes, leaderboard analysis, rationale
