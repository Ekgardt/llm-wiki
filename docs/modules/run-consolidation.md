# MEM-13: does this vault's consolidation earn its cost? A paired measurement

`benchmark/run_consolidation.py` and the tests that exercise them most.

Runs each question twice on one vault — once over the raw captured sessions, once after `episode_consolidation` has appended its durable-item entries — and reports the difference as a paired comparison (`consolidation_score.py`). The per-question work is done by `consolidation_vault.py`, one process per question, for the same reason MEM-10 does it that way: the product resolves its vault roots at import time.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
