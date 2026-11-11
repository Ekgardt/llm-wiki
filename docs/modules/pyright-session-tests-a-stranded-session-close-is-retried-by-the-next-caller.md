# Tests of `scripts/pyright_session.py`

- `tests/test_a_stranded_session_close_is_retried_by_the_next_caller.py`: A session whose close failed is closed again by the next caller that meets it.

`scripts/pyright_session.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_stranded_session_close_is_retried_by_the_next_caller.py -q`.
