# Tests of `scripts/compile_controller.py`

- `tests/test_compile_runtime_identity.py`: Python executable, standard library and execution parameters bind replay.

`scripts/compile_controller.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The shared Python library is the image holding the running interpreter, found by
address, never a configured path
([decision](../research/2026-11-11-the-runtime-library-is-found-by-address.md)).

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_runtime_identity.py -q`.
