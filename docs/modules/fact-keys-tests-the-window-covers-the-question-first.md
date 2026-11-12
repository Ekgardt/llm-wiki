# Tests of `scripts/fact_keys.py`

- `tests/test_the_window_covers_the_question_first.py`: The reader's window covers the parts of the question before the lane score fills it.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_window_covers_the_question_first.py -q`.
