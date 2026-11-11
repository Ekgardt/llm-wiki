# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_definition_under_a_platform_test_is_still_a_definition.py`: Audit 3, B23: a `def` inside an `if`, a `try`, a `with` or a `for` is extracted.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_definition_under_a_platform_test_is_still_a_definition.py -q`.
