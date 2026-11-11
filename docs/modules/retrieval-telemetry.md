# Private, disposable retrieval telemetry stored in rollback-journal SQLite

`scripts/retrieval_telemetry.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/retrieval_telemetry_helpers.py tests/test_a_dropped_telemetry_write_is_recorded.py tests/test_retrieval_telemetry.py tests/test_reviewed_sql_keeps_values_as_data.py tests/test_the_schema_is_created_inside_the_write_transaction.py -q`.
