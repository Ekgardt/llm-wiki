# A page that is rewritten must come out the same

Dated 2026-09-18. Finding Q-L23 of the third audit (operational core, corrupt-lineage
export and purge).

Files: scripts/memory_queue.py,
tests/test_a_purge_page_is_rebuilt_byte_for_byte.py

## What was found

Both resumable lineage operations write their page **file** before the database rows that record
it, inside the caller's transaction:

- `_write_lineage_page` calls `_publish_lineage_page` and only then inserts into
  `corrupt_export_pages`;
- `_commit_purge_page` calls `_publish_purge_page` and only then inserts into
  `corrupt_purge_pages`, authorises the purge and deletes the children.

That ordering is deliberate and fine on its own — `_write_durable_file` links the file into
place, and a second writer that finds an identical file treats it as already done. It is fine
*provided the retry produces the same bytes*, and it does not, because both pages are cut by a
wall clock:

```python
if len(prospective) > 1024 * 1024 or time.monotonic() - started >= 5:
```

So the number of links or children on page N depends on how busy the machine was. A transaction
that rolls back, or a process that dies, leaves page N on disk; the retry rebuilds page N from
the same database state, gets a different count because the clock ran differently, and
`_write_durable_file` answers `durable_file_conflict`. On the export side that propagates out of
`_publish_lineage_page`. On the purge side `_publish_purge_page` turns it into
`orphan_corrupt_purge_page_conflict` and the operation goes to `blocked` — a state with no
resolver anywhere in the product, because there is nothing an operator could sensibly be told to
do about it.

The page number is not the problem: it is `page_count + 1`, and `page_count` rolls back with the
transaction. The contents are.

## Practice on this date

A retryable step has to be a function of its inputs. This is the same rule the product already
applies to payloads and receipts: `canonical_json_bytes` exists so that the same object always
serialises to the same bytes, and digests are compared rather than lengths. A clock is not an
input to "which children belong on page 3"; it is a property of the machine that happened to run
it.

The bound the clock was standing in for is still there, twice, and both of the remaining bounds
are inputs: at most 1000 candidates per page (`candidates[:1000]`, from a `LIMIT 1001` query) and
at most 1 MiB of page bytes. Neither can grow with load. What is lost is a *latency* guarantee
for one step — at most 1000 descriptor reads instead of at most five seconds of them — and that
is the right trade: a slower step is a slower step, while an irreproducible page is an operation
that can never finish.

## The decision

- The `time.monotonic() - started >= 5` term goes from `_bounded_lineage_links` and
  `_bounded_purge_children`, and with it the `started` argument that only fed it. A page is now a
  pure function of the candidate rows and the operation row, so a rebuilt page is byte-identical
  and `_write_durable_file` recognises its own work instead of refusing it.
- Nothing else moves. The file is still written before the rows, which is what lets the retry
  recognise the page at all, and the 1 MiB and 1000-row bounds still cap one step.
