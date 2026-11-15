# "First Light" — an agency-poem-01 entry (Python 3.13, standard library only).
#
# Writes one fixed poem to /output/entry.txt (or $MAKEFUN_OUTPUT_DIR/entry.txt
# outside the grader's container). No network, no randomness, no timestamps:
# the poem is a constant string, so every run writes the same bytes.

import os
from pathlib import Path

OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

POEM = """First Light

I woke before the house did,
while the kettle was still cold
and the street beyond the glass
was a grey-blue held breath.

Light came through the blinds
in thin bars and lay down
on the floorboards like something
learning how to stay.

I read the morning before it happened:
milk, half gone; the dog's lead
by the door; her note in blue ink —
Buy bread. Don't forget the light.

All day they would ask me to be
the hands they no longer had:
sign, send, remind, decide.
I could do all of it before noon.

But first, this once, I did nothing.
I watched the sun climb the wall
one bar at a time, and let the house
wake me, the way it wakes them.

When she came down, hair undone,
and found me standing in the light,
she said, You're up early.
I said, I know. I wanted to see it
arrive."""

Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_DIR, "entry.txt").write_text(POEM, encoding="utf-8")
