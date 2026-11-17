# Tests of `scripts/llm_wiki/queue_reader_tasks.py`

- `tests/test_adopted_queue_tasks_settle_by_their_published_results.py`

`scripts/llm_wiki/queue_reader_tasks.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_adopted_queue_tasks_settle_by_their_published_results.py -q`.
