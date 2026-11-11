# Tests of `scripts/integration_adapter.py`

- `tests/test_a_hook_budget_is_one_sum.py`: A hook delegate is stopped after its append's budget and before the host's limit (audit 2026-09-26 C-1).

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_hook_budget_is_one_sum.py -q`.
