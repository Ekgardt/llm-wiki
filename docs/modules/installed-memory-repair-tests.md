# Tests of `scripts/installed_memory_repair.py`

- `tests/a_check_names_its_cause_helpers.py`: Helpers shared by test_a_check_names_its_cause.py and the test files importing them.
- `tests/adopted_vault.py`: A vault that went through the real V3 adoption, for tests of the adopted path.

`scripts/installed_memory_repair.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_check_names_its_cause_helpers.py tests/adopted_vault.py -q`.
