# A source is referenced by a field, not by a date somewhere in the text

Dated 2026-09-18. Finding Q-L27 of the third audit (operational core, queue).

Files: scripts/memory_queue.py,
tests/test_a_fence_reads_the_payloads_own_fields.py

## What was found

`MemoryQueue._payload_references_source` decides whether a queued task points at the daily
source a fence is protecting, and it decides it like this:

```python
return daily_id in payload_json or source_digest in payload_json
```

Four places act on that answer, on both backends (legacy `MemoryQueue` and the adopted
`_QueueV3CandidateReader`): `_assert_payload_not_fenced` refuses an enqueue or a redrive with
`source_fenced`, `_require_source_unreferenced` refuses to take the fence with
`source_referenced`, `_require_no_source_reference` refuses to finalize inside the fence, and
`referencing_source_tasks` is the operator's listing.

A `daily_id` is a date — `2026-09-18`. Every timestamp this product writes starts with one.
So while `archive_daily` holds a fence over today's log, *every* task whose payload carries a
timestamp of that day matches, and the whole queue refuses new work with `source_fenced` —
including tasks about a completely different page, a compile of a note, a query. Symmetrically,
`acquire_source_fence` sees those same unrelated tasks as references and refuses the fence with
`source_referenced`, so the archive cannot start either. The error the operator sees names the
source correctly and means nothing.

The reverse mistake is available too: any field anywhere in a payload that happens to hold the
day's date — a `window`, a `since`, a free-text note — reads as a reference to the source.

## The payload-kind field contract

This module already states which payload fields carry a source identity, and has since the v2
migration was written. `_SOURCE_IDENTITY_KEYS` is that contract:

| payload key (case-insensitive) | what it contributes |
|---|---|
| `source_path`, `logical_path` | a logical path |
| `source_digest`, `digest`, `hash` | a content digest |
| `daily_id` | a daily id |

`_collect_source_identity_strings` walks a payload — mappings and lists, at any depth — and
gathers the string values under exactly those keys, ignoring every other key and every value
that is not a string. `_distinct_source_identities` adds the one derivation the contract needs:
a payload that names a `daily_id` and no path names the page `knowledge/daily/<daily_id>.md`.
The v2→v3 migration has trusted this reading for every task it moved; the fence is the one
caller that never used it.

## Practice on this date

Substring containment is not a reference: the string `2026-09-18` occurs inside
`2026-09-18T11:04:22Z`, inside a `since` window, and inside a sentence a person typed. MITRE
files this shape as CWE-697, *Incorrect Comparison* — "The product compares two entities in a
security-relevant context, but the comparison is incorrect"
([CWE-697, version 4.20](https://cwe.mitre.org/data/definitions/697.html)) — and a fence that
decides who may write to a source is such a context. The product's own rule says the same thing
in one line: the owner's standing instruction is that behaviour must never be chosen by matching
substrings of text, and the same round's `Q-L14` fix replaced eleven of them in the Markdown
coordinator with types.

The structured reading is also what the rest of this module does. `_queue_v3_source_links`,
`_require_single_source_identity` and the capture-link tables all key on the parsed fields, so
using the parse here removes a second, disagreeing notion of "references" rather than adding
one.

## The decision

- `_payload_references_source` parses the payload and compares its identity **fields**: the task
  references the source when its `daily_id` field equals the fenced daily id, or one of its path
  fields equals `knowledge/daily/<daily_id>.md`, or one of its digest fields equals the fenced
  digest. Nothing else is a reference.
- The fence keeps the *or* it has today rather than requiring path and digest together: a task
  that names the page but an older digest is still work against the source being rewritten, and
  the fence is held for seconds, so the conservative side is the right one.
- A payload that cannot be parsed as JSON counts as referencing every fenced source. That is the
  conservative answer at both call sites — it refuses the enqueue and refuses the fence — and it
  cannot arise from the enqueue path, where the payload has already been canonicalised.
- Two divergences the same finding names are closed with it: the adopted queue's `cancel` of a
  **leased** task now writes the `attempt_history` row that the legacy queue writes, so an
  attempt that was cancelled mid-flight stops vanishing from the record it is counted in.
