# Turn a day of session records into durable knowledge, in the idle window

`scripts/episode_consolidation.py` and the tests that exercise them most.

Sessions are kept verbatim (`knowledge/raw/sessions/`), but keeping is not remembering: nothing reads them, so nothing becomes a page. The 2026 survey of agent memory lists principled consolidation as the first open frontier and describes the shape this vault already has half of — raw episodes in a hot buffer, promoted to durable storage only after validation. Letta reports 18% higher accuracy and 2.5x lower cost per query from moving that work off the query path; here it runs in the nightly pass, where nobody is waiting.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_day_is_consolidated_whole_and_reopened_when_it_grows.py tests/test_a_day_that_failed_is_tried_again.py tests/test_a_lesson_is_one_line_from_a_session_that_holds_its_quote.py tests/test_a_lost_episode_batch_is_asked_again_after_a_fix.py tests/test_a_session_is_filed_under_the_day_it_happened.py tests/test_a_written_batch_is_found_not_asked_again.py tests/test_episode_consolidation.py -q`.
