# Tests of `scripts/installed_memory_repair.py`

- `tests/test_project_store_adopted_coordinator.py`: ProjectStore must open the coordinator adoption made active, not the tombstone.

`scripts/installed_memory_repair.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_project_store_adopted_coordinator.py -q`.
