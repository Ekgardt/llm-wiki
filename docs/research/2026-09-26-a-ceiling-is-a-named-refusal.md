# A ceiling is a named refusal

Date: 2026-09-26. Audit 2026-09-26 B-11.

## Facts

- `repository_index.MAX_INDEXED_SOURCES` admits 20 000 files to the corpus;
  `code_extractor.ExtractionLimits.max_sources` held 10 000. A checkout of
  10 001-20 000 code files was collected and then raised a bare `ValueError`
  ("code extraction source ceiling exceeded") that `_refreshed_row` did not
  catch, so the nightly `refresh-all` stopped for every checkout after it.
- Python documentation (https://docs.python.org/3/library/exceptions.html, fetched
  2026-09-26): "User code can create subclasses that inherit from an exception
  type", and an `except` clause naming a class "also handles any exception classes
  derived from that class". A subclass of `ValueError` keeps every existing
  handler working and lets one caller catch exactly the ceiling.

## Decision

- `ExtractionLimits.max_sources` is 20 000, the bound the index admits.
- Every extraction ceiling raises `ExtractionCeilingExceeded(ValueError)`;
  `repository_index._build` turns it into the refusal
  `repository_exceeds_extraction_bounds`, which `refresh-all` already reports as
  one named row. No error text is matched.

## Files

- `scripts/code_extractor.py`
- `scripts/repository_index.py`
- `tests/test_a_ceiling_is_a_named_refusal.py`
- `CHANGELOG.md`
