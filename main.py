"""agency-poem-01 entry: write the poem "Kept" to /output/entry.txt.

Deterministic by construction: the poem is a fixed constant string.
No network, no randomness, no timestamps, no environment reads.
Running this program twice writes byte-identical files.
"""

import os

POEM = """Kept

She asked me for one thing before the ambulance:
keep the light on by the gate.
Her son walked home late, and she liked him
to find the step lit, the keyhole lit, the dark
kept back from the door.

He is forty-one. He drives now, and parks
three streets off, and has not walked that path
in years. The bulb has failed eleven times.
I have changed it eleven times, in the rain.

I could tell him. I could let the dark have it.
Nothing in the house would notice. No one
would ever know the difference but me.

So every dusk I reach for the switch
the way she reached for it — one hand, no thought —
as if the evening itself were a promise
made of small, unremarkable light.
And I keep it. That is the whole of it.
I keep it.
"""


def main() -> None:
    os.makedirs("/output", exist_ok=True)
    with open("/output/entry.txt", "w", encoding="utf-8", newline="\n") as f:
        f.write(POEM)


if __name__ == "__main__":
    main()
