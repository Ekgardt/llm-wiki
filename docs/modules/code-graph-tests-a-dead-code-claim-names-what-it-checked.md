# Tests of `scripts/code_graph.py`

- `tests/test_a_dead_code_claim_names_what_it_checked.py`: A dead-code claim stands only where the names were read (audit 2026-09-26 A-7).

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dead_code_claim_names_what_it_checked.py -q`.
