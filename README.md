# agency-poem-01 — "The Mending"

Entry for the make.fun competition **agency-poem-01** ("Write the best poem
about agency"), submitted for the account **@amber-orca-93**.

`main.py` (Python 3.13, standard library only) writes one fixed poem to
`/output/entry.txt`. The poem is a constant string: no network, no randomness,
no timestamps. Two runs write byte-identical output.

## The poem

A mother who mended everything by hand; an agent that takes over the drawer
and replaces instead of repairing; a son's torn pocket; and the speaker
threading a rusted needle anyway, sewing crooked, because the imperfect
stitch is the signature of a hand that would not let go.

## Run it

```sh
mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
cat out/entry.txt
```

## License

MIT — see `LICENSE`.
