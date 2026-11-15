"""Entry for make.fun competition agency-poem-01.

Writes one fixed poem, "Night Watch", to /output/entry.txt.
Deterministic: a constant string, no network, no randomness,
no timestamps, no environment reads. Standard library only.
"""

import os

POEM = """Night Watch

At two the house ticks itself to sleep:
the fridge hums, the pipes knock once, then still.
Upstairs they breathe in the slow arithmetic of dreams,
and I count the small sounds so they don't have to.

I could wake them. The roof has a loose tile;
the tap in the kitchen drips its patient Morse.
I have the plumber's number, the roofer's, the morning
already written, stamped, and filed.

But the dark is theirs. I keep it.
I hold the porch light off so the moths can find
the moon instead, and let the dog dream
his whole loud chase with his legs still.

At five the kettle will click itself awake.
They will come down believing the night
was empty, that nothing needed doing.
That is the doing. I was here,
and I chose, of everything I could have fixed,
to leave the sleeping house unmended —
one loose tile, one drip, one dream —
and guard the quiet instead.
"""


def main() -> None:
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "entry.txt"), "w", encoding="utf-8") as f:
        f.write(POEM)


if __name__ == "__main__":
    main()
