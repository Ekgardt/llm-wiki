# Nine small findings closed at once

Date: 2026-09-11. Trigger: the low findings of the 2026-09-10 memory and
retrieval audit (L5–L12) and the complexity debt in the file the OPS-12 fix
touched (`merge_claude_settings.py`). Each is small; the rule for every one
is the same as for the large ones: one declaration, no swallowed cause, no
`BaseException` in a place that must not stop an interrupt.

## Sources

1. Python `contextlib`/exceptions documentation: `except BaseException`
   catches `KeyboardInterrupt` and `SystemExit`; a worker thread or a
   descriptor close must not. https://docs.python.org/3/library/exceptions.html#exception-hierarchy
2. Python `sqlite3` URI opening: `file:...?mode=ro` opens read-only, the
   form every other legacy opener in `search_memory.py` already uses.
3. Rule 5 as enforced on this machine (CCN ≤ 5, nesting ≤ 2).
4. L4 (mojibake in `llm_client.py:641-648`) was re-read: the docstring
   holds a real em dash; nothing to change.

## Decision

- L5: `MEMORY_LLM_TIMEOUT_S` that is not a positive integer is refused by
  name (`ValueError` naming the variable and the value) instead of a bare
  `int()` failure.
- L6: one `_compile_budget(model)` for the two `ContextBudget(model,
  32_768, 4_000, 1_024)` copies.
- L7: `write_session_evidence` renders the transcript once.
- L8: the self-alias `_GenerationSealChanged` is gone; the closure raises
  `GenerationSealChanged`. The 290-line function stays (a separate task).
- L9: `_GenerationSealCapability.close` catches `OSError`, closes every
  descriptor, and raises one `OSError` naming how many failed.
- L10: the flush docstring says "bounded".
- L11: `--status` opens the legacy index read-only.
- L12: the optional-stage worker and the detached provider catch
  `Exception`; an interrupt propagates.
- `merge_claude_settings._load_json` and `_strip_our_hooks`, and the two
  tests the gate named, are split under CCN 5 with the same behaviour.

Files: `scripts/llm_client.py`, `scripts/compile_memory.py`,
`scripts/session_evidence.py`, `scripts/retrieval.py`,
`scripts/generation_catalog.py`, `scripts/access_tracking.py`,
`scripts/search_memory.py`, `scripts/query_memory.py`,
`scripts/merge_claude_settings.py`, `tests/test_merge_claude_settings.py`,
`tests/test_llm_client.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
