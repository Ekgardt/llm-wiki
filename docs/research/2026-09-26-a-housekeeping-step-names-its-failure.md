# A housekeeping step names its failure

Date: 2026-09-26. Audit 2026-09-26, regression list item 8.

## What was wrong

After the counted steps, the nightly pass runs two housekeeping steps directly:
report retention (`_prune_reports`) and the code update (`_update_code`). Neither
was guarded. `_update_code` says an update "is never a reason to fail the night",
yet its `load_state`/`update_state` call (state lock timeout, `OSError`) or an
exception from `update_checkout` escaped, ended the pass with a terminal error, and
recorded the night as failed. An exception in retention also skipped the update.

## Decision

`scheduled_nightly._housekeeping(log, label, step)` runs one of these steps; an
`Exception` is named in the nightly report (`<label> failed: …`, through
`describe_error`) and returns False. A failed retention counts as one failure of
the night; a failed update is named only, as its own contract says. Both always
run. `BaseException` (interrupt, exit) is not caught.

## Source

PEP 20, The Zen of Python, https://peps.python.org/pep-0020/, fetched 2026-09-26:
"Errors should never pass silently." / "Unless explicitly silenced."

The update's error is silenced only as far as the night's verdict is concerned,
and explicitly: it is written into the report by name.

## Files

- `scripts/scheduled_nightly.py`
- `tests/test_a_housekeeping_step_names_its_failure.py`
