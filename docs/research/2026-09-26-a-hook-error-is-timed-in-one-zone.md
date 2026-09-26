# A hook error is timed in one zone

Date: 2026-09-26. Audit 2026-09-26 B-25.

## Facts

- Four writers of `logs/hook-errors.log` stamped local wall-clock time without an
  offset (`integration_adapter._log_checkpoint_error`,
  `session_end_project_tag`, `session_start_project_state`, `memory_state`'s
  corrupt-state note). `doctor._hook_error_is_live` compared that stamp with an
  aware UTC `now` by attaching UTC to it, so west of UTC a fresh error read as
  hours old and was called history, and east of UTC an old one as fresh.
- Python documentation (https://docs.python.org/3/library/datetime.html,
  `datetime.astimezone`, fetched 2026-09-26): "If self is naive, it is presumed to
  represent time in the system time zone", and without arguments the result
  carries "the zone name and offset obtained from the OS".

## Decision

- Every writer stamps `datetime.now().astimezone().isoformat(timespec="seconds")`:
  the same local reading, now with its offset.
- The reader treats a stamp without an offset as local time (`astimezone()`),
  which is what the lines already in the log meant.

## Files

- `scripts/integration_adapter.py`
- `scripts/session_end_project_tag.py`
- `scripts/session_start_project_state.py`
- `scripts/memory_state.py`
- `scripts/doctor.py`
- `tests/test_a_hook_error_is_timed_in_one_zone.py`
- `CHANGELOG.md`
