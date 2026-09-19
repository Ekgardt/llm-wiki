# Four small capture corrections

Dated 2026-09-17. Items of finding C-F15 of the third audit (low, each confirmed by reading and
checked again here). The research before the fix. The items of C-F15 that are not here are left
to the owner and listed in the fix report.

## What was found

1. `user_prompt_counts` in `run/state.json` gains one key per session and nothing ever removes
   one. Readers of that file refuse it above 256 KiB (`memory_state.MAX_STATE_TARGET_BYTES` is
   192 KiB for that reason), so an unbounded map in it is a slow route to a blind health check.
2. `integration_adapter._trim_reducers` removes one entry when the reducer map is over 128,
   while one commit can add several, so the bound drifts upward.
3. A transcript under the evidence bound is decoded strictly (`read_stable_utf8`), while a
   transcript over it is decoded with `errors="ignore"`. One stray byte loses a short session
   whole and costs a long one nothing.
4. `integrations/README.md` says nothing in the product calls `heartbeat_record.py`. The
   adapter runs it on every session start and on every session end that has no transcript
   (`integration_adapter._record_activity`).

## Practice on this date

- A map keyed by something that keeps arriving needs an eviction rule where it is written;
  the state file's other growing maps already have one (`capture_operation`, 100 entries).
- For text that is evidence rather than a program, a byte that does not decode is replaced
  and the rest is kept: the `replace` error handler — "On decoding, use � (U+FFFD, the
  official REPLACEMENT CHARACTER)" ([Python codecs, error handlers](https://docs.python.org/3/library/codecs.html#error-handlers),
  fetched 2026-09-17). `ignore` hides that anything was wrong; `strict` loses the session.

## The decision

1. The prompt counter keeps the 200 sessions counted most recently; a session that prompts
   again moves to the end.
2. `_trim_reducers` removes the oldest entries until the map is back at 128.
3. Both transcript reads decode with `errors="replace"`.
4. The README says what calls the helper.

Files: `scripts/user_prompt_capture.py`, `scripts/integration_adapter.py`,
`integrations/README.md`, `tests/test_four_small_capture_corrections.py`
