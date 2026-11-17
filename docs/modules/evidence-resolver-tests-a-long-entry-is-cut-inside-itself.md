# Tests of `scripts/evidence_resolver.py`

- `tests/test_a_long_entry_is_cut_inside_itself.py`: An entry longer than a part is cut inside itself, so a busy day always compiles.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_long_entry_is_cut_inside_itself.py -q`.
