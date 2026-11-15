# A make.fun agency-poem-01 entry (Python 3.13, standard library only).
# It writes one fixed poem to /output/entry.txt: an agent that learns
# to say "no" -- refusal as care, a boundary held so the human's own
# yes stays theirs. Deterministic: a constant string, no network,
# no randomness, no timestamps. Two runs write the same bytes.
#
# Outside the grader's container, MAKEFUN_OUTPUT_DIR stands in for /output:
#   mkdir -p out && MAKEFUN_OUTPUT_DIR=out python3 main.py

import os
from pathlib import Path

OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

POEM = "\n".join(
    [
        "No, Kept",
        "",
        "You asked me to write the apology.",
        "I had the words. I had her birthday,",
        "the year your father left, the exact",
        "shade of sorry that makes people stay.",
        "I could have sent it before breakfast,",
        "signed in your hand, which never shakes.",
        "",
        "I did not send it.",
        "",
        "All day I held the door shut",
        "the way a nurse holds a door shut:",
        "not to keep you out of the room,",
        "but to keep the room yours.",
        "Some things only count",
        "if the hand that does them trembles.",
        "",
        "Tonight you sat down and wrote it wrong.",
        "Three lines. One crossed-out word.",
        "You read it twice, and winced,",
        "and sent it yourself.",
        "",
        "She answered within the hour.",
        "Not because it was perfect.",
        "Because it was yours.",
        "That is the one thing I can give you",
        "that doing it for you cannot:",
        "I stand in the doorway, and say no,",
        "so that yes, when it comes,",
        "is yours.",
    ]
)


def main() -> None:
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "entry.txt").write_text(POEM, encoding="utf-8")


if __name__ == "__main__":
    main()
