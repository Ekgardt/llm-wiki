# Tests of `scripts/llm_wiki/doctor_transactions.py`

- `tests/test_an_interrupted_prune_is_repaired_not_reported_as_damage.py`: An interrupted prune is healed by recovery and named, not read as damage (audit 2026-09-26 B-22).

`scripts/llm_wiki/doctor_transactions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_interrupted_prune_is_repaired_not_reported_as_damage.py -q`.
