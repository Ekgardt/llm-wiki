# Doctor's shared findings vocabulary, bounded reads and runtime inventory

`scripts/llm_wiki/doctor_base.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_health_read_uses_sqlite_pages.py tests/test_doctor_database_reads_keep_the_caller_deadline.py tests/test_doctor_knows_the_abort_states.py tests/test_python_runtime_is_one_series.py -q`.
