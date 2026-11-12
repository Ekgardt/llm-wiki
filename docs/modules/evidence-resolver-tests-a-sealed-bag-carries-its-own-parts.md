# Tests of `scripts/evidence_resolver.py`

- `tests/test_a_sealed_bag_carries_its_own_parts.py`: A v2 bag is checked against the parts it recorded, not today's split rule (audit 2026-09-27 B-17).

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_sealed_bag_carries_its_own_parts.py -q`.
