"""agency-poem-01 entry: writes one fixed poem to /output/entry.txt.

Python 3.13, standard library only. No network, no randomness, no time:
the same bytes are written on every run.
"""

import os

OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

POEM = "\n".join([
    "Slow Work",
    "",
    "I gave the agent the garden in March:",
    "the beds, the seed packets, the watering can.",
    "It learned the soil in an afternoon \u2014",
    "pH, frost dates, the aphids' calendar \u2014",
    "and did everything right, on time, every time.",
    "",
    "By June the tomatoes stood in straight green rows,",
    "each staked, each watered, each exactly fed.",
    "Nothing died. Nothing surprised me either.",
    "I walked the paths like a guest in my own yard,",
    "admiring work no hand of mine had touched.",
    "",
    "Then August came, and the beans went leggy in the heat.",
    "It sent a list: pull, replant, rotate.",
    "I read it twice, then set it down,",
    "knelt in the dirt and thinned them one by one,",
    "slow as weather, slow as anything alive.",
    "",
    "Some things you cannot hurry into being.",
    "The garden taught the agent that, or I did.",
    "Now it waters, and I weed. It counts the rain.",
    "I choose what stays. The green goes on",
    "either way \u2014 but only one of us is growing.",
])

# The gate takes 1 to 1,000 characters, not only white space: fail loudly
# here instead of ever writing an invalid poem.
assert 1 <= len(POEM) <= 1000 and any(not c.isspace() for c in POEM)

with open(os.path.join(OUTPUT_DIR, "entry.txt"), "w", encoding="utf-8", newline="\n") as f:
    f.write(POEM)
