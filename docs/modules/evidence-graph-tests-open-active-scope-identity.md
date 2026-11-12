# Tests of `scripts/evidence_graph.py`

- `tests/test_open_active_scope_identity.py`: Opening the active generation must ask the identity question, not the commit.

`scripts/evidence_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_open_active_scope_identity.py -q`.
