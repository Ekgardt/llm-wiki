# Join a code symbol to the decisions that shaped it and their session sources

`scripts/provenance_join.py` and the tests that exercise them most.

MEM-16, approved 2026-08-28. The competitors hold one half each: code-graph tools have no memory of why, memory systems have no graph of what. This vault holds both, and the join is the question an operator actually asks: "why is this code like this?" — symbol → decision pages that name it → the daily and session evidence those pages cite. Research: `docs/research/2026-08-28-code-decision-session-join.md`.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_provenance_join.py -q`.
