"""The labelled retrieval set (RET-02): which memory answers which query, which one must NOT, and unrelated filler so that "excluded from the top 3" means something.

`gold` and `distractor` name a scenario (the memory it produced); `fillers` are saved explicitly by an administrator so the workspace has enough unrelated memories."""

from __future__ import annotations

FILLERS = [
    "Sarah renewed her passport in March and keeps it in the hallway drawer.",
    "Sarah's dog is called Biscuit and is afraid of thunderstorms.",
    "Sarah's team ships a quarterly release on the last Friday of the quarter.",
    "Sarah is allergic to shellfish and avoids restaurants that specialise in seafood.",
    "Sarah's sister lives in Lisbon and they call each other on Sundays.",
    "Sarah books conference travel through the Acme travel desk.",
]

# (query, gold scenario, distractor scenario)
QUERIES = [
    ("starting a business", "mem_evt003", "mem_cyclist"),
    ("entrepreneurship", "mem_evt003", "mem_cyclist"),
    ("weekend cycling club ride", "mem_cyclist", "mem_evt003"),
    ("how should options be compared when advising", "mem_evt004", "mem_cyclist"),
]
