# One bad generation does not stop the prune

Date: 2026-09-25. Audit item C-29 (`docs/AUDIT-2026-09-25-full.md`), and the same shape in
`install_models` (from the C-28 finding).

## Facts (checked in code and by test)

- `prune_generations._discard_abandoned` sized the tree (`_directory_bytes`) before its `try`;
  a tree over `MAX_GENERATION_ENTRIES` (4096) raised `ValueError` and ended the whole weekly step
  instead of one `ERROR:` line. Both removal paths also let `sqlite3.Error` through.
- `install_models.main` fetched models one after another with no per-model boundary: offline,
  the first `snapshot_download` raised and the second model was never tried. In
  `huggingface_hub` 1.23.0 `HfHubHTTPError` derives from `OSError`, `httpx.ConnectError` does not
  (checked in this environment).

## Source

- Python tutorial, "Errors and Exceptions", https://docs.python.org/3/tutorial/errors.html
  (fetched 2026-09-25), section 8.3: "Handlers only handle exceptions that occur in the
  corresponding _try clause_, not in other handlers of the same `try` statement." The sizing call
  sat outside the `try` clause, so no handler could catch what it raised.

## Decision

- Both prune removals go through one guarded helper (`_attempted`) whose `try` covers the sizing
  and the removal, catching `OSError`, `ValueError`, `TimeoutError`, `RuntimeError` and
  `sqlite3.Error`; each failure is that generation's `ERROR:` line and the pass goes on.
- `install_models` gives each model its own boundary: a failed fetch (`OSError`, or the transport's
  `httpx.HTTPError` when installed) is that model's `unreachable` outcome; the next model is still
  tried and the run still exits incomplete.

## Files

- `scripts/prune_generations.py`
- `scripts/install_models.py`
- `tests/test_one_bad_generation_does_not_stop_the_prune.py`
- `tests/test_a_bad_model_file_is_fetched_again.py`
- `CHANGELOG.md`
