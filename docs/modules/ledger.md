# Compatible ledger entry and runtime generation/Markdown coordination

`scripts/ledger.py` and the tests that exercise them most.

Deterministic rules have one implementation in llm_wiki.ledger_core. Explicit exports keep existing consumers and module-qualified historical objects usable.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_ledger_posts_once_and_counts_by_code.py tests/test_the_ledger_rides_in_the_generation.py -q`.
