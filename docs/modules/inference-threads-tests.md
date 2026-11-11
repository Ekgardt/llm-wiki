# Tests of `scripts/inference_threads.py`

- `tests/test_no_model_running_at_exit.py`: A closing server waits for model inference to reach a safe point before it exits.
- `tests/test_the_shared_server_settles_inference_before_it_exits.py`: Audit 3, A8: the HTTP transport leaves no model mid-inference at exit either.

`scripts/inference_threads.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_no_model_running_at_exit.py tests/test_the_shared_server_settles_inference_before_it_exits.py -q`.
