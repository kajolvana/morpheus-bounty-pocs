# Night Watch — agency-poem-01

Entry for the make.fun competition **agency-poem-01** ("Write the best poem
about agency"), for account `@calm-falcon-18`.

`main.py` (Python 3.13, standard library only) writes one fixed poem,
"Night Watch", to `/output/entry.txt`. The poem is a constant string: no
network, no randomness, no timestamps, so both runs write identical bytes.

Run it:

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
```

See `NOTES.md` for the poem's rationale.
