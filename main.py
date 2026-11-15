"""agency-poem-01 entry: writes one fixed poem to /output/entry.txt.

Deterministic by construction: the poem is a constant string. No network,
no randomness, no timestamps, no environment-dependent output. Run it twice
and both runs write the same bytes.

Outside the grader's container, MAKEFUN_OUTPUT_DIR stands in for /output:
  mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py
"""

import os
from pathlib import Path

OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

POEM = (
    "The Mending\n"
    "\n"
    "My mother mended everything by hand —\n"
    "the knee of my jeans, the hem of her dress,\n"
    "a shirt my father wore till the elbows shone.\n"
    "She threw nothing away a needle could save.\n"
    "The thimble lived on her middle finger\n"
    "like a second skin.\n"
    "\n"
    "When her hands went stiff the agent took the drawer.\n"
    "It ordered the coat, the jeans, the shirt,\n"
    "new in a day, in my size, in hers.\n"
    "It never dropped a stitch. It never pricked a thumb.\n"
    "The closet filled. Nothing frayed. Nothing waited.\n"
    "\n"
    '"Shall I order a new one?" it asked last night,\n'
    "when my son tore the pocket of his jacket.\n"
    "I found her sewing box under the bed,\n"
    "the spools gone soft with dust, the needles rusted shut.\n"
    "\n"
    "I threaded one anyway, hands shaking,\n"
    "and sewed the pocket back — crooked, loose,\n"
    "the knots showing through.\n"
    "\n"
    "He wore it to school this morning.\n"
    "A stitch can hold, or it can say:\n"
    "someone sat here, in the lamplight,\n"
    "and would not let this go."
)


def main() -> None:
    out = Path(OUTPUT_DIR) / "entry.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(POEM, encoding="utf-8")


if __name__ == "__main__":
    main()
