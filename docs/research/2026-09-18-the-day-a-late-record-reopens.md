# The day a late record reopens

Dated 2026-09-18. The work of finding M-A13 of the third audit crossed midnight; this note
carries its decision forward for the files written today. The research and the decision are
in `docs/research/2026-09-17-a-day-is-consolidated-whole-and-reopened-when-it-grows.md`;
what follows is what changed after it, and why the shape held.

Files: `scripts/episode_consolidation.py`,
`tests/test_a_day_is_consolidated_whole_and_reopened_when_it_grows.py`.

## What was found, after the fix was written

- With the per-day ceiling turned into a per-run bound, a day of any size is now consumed
  over as many nights as it needs. The measured cost is unchanged per night: at most twenty
  provider calls, the number the nightly step's budget was sized for.
- The reopening rule needs a compatibility answer for days consolidated before it existed.
  Those records carry no `record_set` digest. Treating "no digest" as "reopen" would put
  every historic day back into the queue: on the owner's vault the imported history holds
  days with more than a hundred sessions each, which is a provider call per batch of them,
  paid for nothing — the records were already read. So a day with no digest stays closed,
  and only days closed from now on carry the digest that can reopen them.

## Practice on this date

- This is the ordinary rule for adding a field to persisted state. "BACKWARD compatibility
  means that consumers using the new schema can read data produced with the last schema",
  and for an added optional field "the default value specified in the new schema will be
  used for the missing field when deserializing the data encoded with the old schema"
  ([Schema Evolution and Compatibility, Confluent](https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html)).
  The default here is "closed", because that is what the older version meant when it wrote
  the record without the field.
- The vault's own contract points the same way: runtime SQLite and state are "coordination/
  derived state, never a knowledge source" (`CLAUDE.md`, Stage 2 operational contract), so
  an absent field is a gap in a cache, never evidence that work was left undone.

## The decision

- Unchanged from yesterday's note, with one addition made explicit: an absent `record_set`
  digest means the day is closed, and the test file named above pins that, together with
  the per-run bound and the reopening.
