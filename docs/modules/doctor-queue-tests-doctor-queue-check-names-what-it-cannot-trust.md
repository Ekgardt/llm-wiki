# Tests of `scripts/llm_wiki/doctor_queue.py`

- `tests/test_doctor_queue_check_names_what_it_cannot_trust.py`

`scripts/llm_wiki/doctor_queue.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_queue_check_names_what_it_cannot_trust.py -q`.
