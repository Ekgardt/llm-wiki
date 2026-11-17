# Tests of `scripts/session_start_context.py`

- `tests/test_less_noise_at_session_start.py`: Session start carries no word-match impact guesses, no half words, and leaves its debug copy.

`scripts/session_start_context.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_less_noise_at_session_start.py -q`.
