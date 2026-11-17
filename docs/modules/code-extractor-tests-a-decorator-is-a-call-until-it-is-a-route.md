# Tests of `scripts/code_extractor.py`

- `tests/test_a_decorator_is_a_call_until_it_is_a_route.py`: Audit 3, B25: decorators, route verbs and one expression too deep to render.

`scripts/code_extractor.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_decorator_is_a_call_until_it_is_a_route.py -q`.
