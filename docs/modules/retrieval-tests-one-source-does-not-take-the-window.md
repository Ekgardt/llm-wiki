# Tests of `scripts/retrieval.py`

- `tests/test_one_source_does_not_take_the_window.py`: One source does not take the window while another covers a part of the question.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_source_does_not_take_the_window.py -q`.
