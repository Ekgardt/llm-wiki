# Re-create pages a refused transaction wrote and nothing wrote since

`scripts/repair_refused_page_creation.py` and the tests that exercise them most.

A compile that plans several files applies them as one transaction. When one of the shared targets — `knowledge/log.md`, most often — is appended to by another writer between the plan and the apply, the precondition fails and the whole transaction is refused. The pages the compile had already produced are never written, and the compile does not run again: the daily log it read is already marked compiled.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_refused_page_is_still_owed.py -q`.
