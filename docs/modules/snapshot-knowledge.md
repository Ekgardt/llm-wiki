# Keep a second copy of the memory, outside the vault, with its history

`scripts/snapshot_knowledge.py` and the tests that exercise them most.

Measured 2026-09-02: `knowledge/` is 20 MB and about 2.8 MB of it changes in a day. Of 116 pages only 83 are in git, and of 15 daily logs only 3 — the rest is private by design and therefore exists in exactly one place, on one disk. The undo trail was not a second copy either: it held before-and-after images of individual writes, and it has just been cut from 5.0 GB to 307 MB because a month of them was an ad-hoc backup nobody could restore a day from.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_snapshot_says_when_it_last_looked.py tests/test_every_git_call_has_a_deadline.py tests/test_the_memory_keeps_a_second_copy.py -q`.
