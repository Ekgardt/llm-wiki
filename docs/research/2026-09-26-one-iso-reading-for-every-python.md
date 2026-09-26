# One ISO reading for every Python

Date: 2026-09-26. Audit 2026-09-26 C-4 (B-5 of 2026-09-25).

## Facts

- `contradiction_pipeline._instant` replaced `Z` and called
  `datetime.fromisoformat`. On Python 3.10 a fraction of seconds that is not 3 or 6
  digits (`.5`, `.1234`) raised `ValueError`; pages and models write such values.
  Reproduced on 3.10: `test_the_contradiction_pipeline_reads_a_short_fraction`
  fails on the old code.
- Python documentation (https://docs.python.org/3/library/datetime.html,
  `datetime.fromisoformat`, fetched 2026-09-26): "Changed in version 3.11:
  Previously, this method only supported formats that could be emitted by
  date.isoformat() or datetime.isoformat()."
- Three separate helpers already normalised only the `Z` (`doctor._iso_text`,
  `session_start_context._iso_text`, `scheduled_nightly._offset_spelling`).

## Decision

- `scripts/iso_time.py` holds one reading: `normalized_iso` (a trailing `Z` as
  `+00:00`, a fraction padded or cut to six digits) and `parse_instant` (aware; no
  zone means UTC). `contradiction_pipeline._instant` uses it.
- The other parsers read timestamps this runtime writes itself (whole seconds or
  six-digit microseconds), so they are left as they are here; moving them onto the
  shared reader is recorded as follow-up rather than done blind.

## Files

- `scripts/iso_time.py`
- `scripts/contradiction_pipeline.py`
- `tests/test_one_iso_reading_for_every_python.py`
- `CHANGELOG.md`
