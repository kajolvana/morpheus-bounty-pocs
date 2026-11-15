"""Write one poem about agency to /output/entry.txt.

The poem is a fixed constant: no network, no randomness, no clock,
no environment-dependent output. Two runs write the same bytes.
"""

import os

POEM = """The Thread

You live six thousand miles east,
where my morning is your midnight.
The agent keeps your hours for me:
it waters your fern, pays the landlord,
files the forms, and knows the weather
over your street before I wake.

Every night it offers to do more —
to write the letter, to say goodnight,
to be the one who crosses for me.
I give it the only job it cannot hurry:
wait.

It waits at the window with the envelope,
the stamp unlicked, the address in my hand.
It holds the thread — one end here,
one end folded in your pocket — and keeps it taut
across the ocean all the long night.

When the plane lifts off, it does not wave.
It only watches the thread go slack,
then sing tight again, eleven hours on,
when I land, tired and unshaven,
and you open the door and know
these hands, at last, are mine."""


def main() -> None:
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "entry.txt")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(POEM)


if __name__ == "__main__":
    main()
