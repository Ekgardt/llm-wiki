# Tests of `scripts/pyright_session.py`

- `tests/test_a_startup_that_ran_out_of_time_is_tried_again_later.py`: A start that ran out of time is tried again; a broken install is not.

`scripts/pyright_session.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_startup_that_ran_out_of_time_is_tried_again_later.py -q`.
