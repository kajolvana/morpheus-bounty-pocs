# agency-poem-01 entry (Python 3.13, standard library only): it writes one fixed poem,
# "The Keeper", to /output/entry.txt. The poem is a constant string: no network, no
# randomness, no timestamps, no input read, so every run writes the same bytes.
# Outside the grader's container, MAKEFUN_OUTPUT_DIR stands in for /output:
#   mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py

import os
from pathlib import Path

OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

POEM = """The Keeper

It has my wife's laugh saved from 2019,
flour on her hands in the kitchen, exactly —
never a breath out of place, never less.

It keeps the date we met, the song, the rain,
the name of every dog we ever lost.
It keeps the argument too, the last one,
word for word. The door. The silence after.

I forget now. Names go first, then faces.
Some mornings I stand in the kitchen
and cannot say why I came in.

It could tell me. It doesn't.
It says the small losings are mine to keep.
But the laugh it will not let go of,
nor the morning she said yes,
nor the way she said my name
when she was not angry.

I asked it once to lose the last day,
the hospital light, the held breath.
It said no.

Someone has to remember it truly,
it said, so the rest of her
can stay the way you loved her.
So I let it carry that one.
It remembers, so I can forget —
and forgetting, for once,
is something I chose."""

Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_DIR, "entry.txt").write_text(POEM, encoding="utf-8")
