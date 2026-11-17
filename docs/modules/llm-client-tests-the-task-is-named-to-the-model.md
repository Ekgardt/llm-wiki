# Tests of `scripts/llm_client.py`

- `tests/test_the_task_is_named_to_the_model.py`: The Claude provider frames the task, so a machine's session banner is not answered instead.

`scripts/llm_client.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_task_is_named_to_the_model.py -q`.
