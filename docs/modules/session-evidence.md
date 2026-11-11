# Keep the session itself, not only what a classifier thought of it

`scripts/session_evidence.py` and the tests that exercise them most.

Measured on this vault's own sessions, the classifier answered "nothing worth keeping" 39 times out of 40. The fix is not a better classifier: a 2026 ablation that varied only the stored representation found verbatim conversation beating extracted artifacts by 15.9 points on LoCoMo and 22.0 on LongMemEval-S, because extraction commits to relevance before the question exists. So every captured session now leaves a redacted, searchable copy of itself, and the classifier decides only whether the session also deserves a compiled page.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_codex_session_is_read_as_a_conversation.py tests/test_a_long_session_keeps_its_first_turns.py tests/test_a_subagents_report_is_kept_with_its_session.py tests/test_a_tool_line_is_redacted_before_it_is_cut.py tests/test_every_session_record_is_redacted.py tests/test_longmemeval_benchmark.py tests/test_one_day_for_a_session_record.py tests/test_session_evidence.py -q`.
