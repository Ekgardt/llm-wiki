# L0/L1/L2 tiered knowledge loading — progressive disclosure

`scripts/build_tiers.py` and the tests that exercise them most.

Generates multi-level summaries for each knowledge page so agents can load the right amount of context without overpaying for tokens.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_build_tiers.py -q`.
