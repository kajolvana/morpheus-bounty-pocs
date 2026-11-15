#!/usr/bin/env python3
"""agency-poem-01 entry: writes one fixed poem to /output/entry.txt.

Deterministic by construction: the poem is a module constant, there is no
network access, no randomness, no clock, and no environment-dependent
output. Every run writes the same bytes.
"""

import os

OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

POEM = """Night Shift

The agent watches all forty beds at once.
It knows the numbers before they fall,
calls the doctor, signs the doses,
never blinks, never tires.
It does the night better than I ever did.

At three the man in 4B asks for water.
No family. No one coming.
The agent has logged his thirst,
flagged the intake, adjusted the drip.
There is nothing left for me to do.

So I sit. I hold the cup
while he drinks, slow, the way you drink
when it hurts. He tells me about the dog,
a brown one, gone twenty years.
He laughs. I laugh.
The monitor keeps its green.

When his breathing changes, the agent
will call the team and do everything right.
It cannot sit here.
So I do."""


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "entry.txt"), "w", encoding="utf-8", newline="") as f:
        f.write(POEM)


if __name__ == "__main__":
    main()
