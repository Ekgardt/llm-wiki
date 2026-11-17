# Append to the daily log under its lock: the one writer every capture path shares

`scripts/daily_log_append.py` and the tests that exercise them most.

`locked_append` and `locked_append_once` add a block to a day's log with an operation marker, so a retry finds the block instead of writing it twice; `append_daily` files a block under the day it belongs to. The capture hooks, the queue worker, the MCP server and episode consolidation all write through here.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_block_opens_one_entry.py tests/test_a_redelivery_after_midnight_is_recognised.py tests/test_a_redriven_flush_appends_nothing_new.py tests/test_a_tool_breadcrumb_waits_in_the_background.py tests/test_every_hook_writer_gives_up_before_its_host.py -q`.
