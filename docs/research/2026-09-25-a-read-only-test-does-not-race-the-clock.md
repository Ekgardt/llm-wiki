# A read-only test does not race the clock

Date: 2026-09-25. Found by the clean run of 8df5bc05 (1 of 9380 failed).

## Facts (traced from the failure)

- `test_open_existing_read_only_avoids_catalog_setup_writes` forbids `Path.mkdir` and then reads
  the active generation. The read validates the generation's artifacts; `verified_artifacts`
  trusts a remembered verdict only when the file is strictly older than the verdict (git's racy
  rule), so a generation published, activated and read inside one timestamp tick is re-verified
  and the reader saves the verdict cache — a `mkdir` the test forbids. Whether that happens was
  decided by timing alone: the file passed three times in a row in isolation.
- Unrelated CI flakes in the same period, recorded for the final sweep: Windows
  `test_archive_winning_finalization_race…` (`database is locked` on a loaded runner) and Windows
  `test_cursor_wrap_reaches_new_candidate_behind_cursor` (a 150 s run whose page transactions hit
  their deadline and said so).

## Source

- git, "Use of index and Racy Git problem", https://git-scm.com/docs/racy-git (fetched
  2026-09-25): "If the modification that follows it happens very fast so that the file's
  `st_mtime` timestamp does not change, after this sequence, the cached stat information the index
  entry records still exactly match what you would see in the filesystem, even though the file
  `foo` is now different."

## Decision

- The test backdates the published generation's files by 60 s before activation, as any real
  generation is older than the reader's verdict; the product rule is unchanged.

## Files

- `tests/test_generation_catalog.py`
