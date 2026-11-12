# Tests of `scripts/generation_maintenance.py`

- `tests/test_the_legacy_fence_keeps_its_pace.py`: The legacy maintenance row renews at its own pace after the guard learned per-lease pace.

`scripts/generation_maintenance.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_legacy_fence_keeps_its_pace.py -q`.
