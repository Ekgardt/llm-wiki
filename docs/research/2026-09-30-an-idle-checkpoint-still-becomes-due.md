# An idle checkpoint still becomes due

Investigated 2026-09-30 against LLM Wiki 5.0.0, f02c5265.

The installed backlog drain left eleven observations in eight projects, without
an exception. One project's last committed checkpoint was 14:39:38.465826;
its last queued event was 14:40:07.641697 on September 28. Their difference
stays below the existing thirty-second debounce interval forever. Replaying
that event timestamp on September 30 does not advance time.

The complete path is native observation, durable pending state, claim of an
ordered prefix, reducer replay, debounce decision, immutable in-flight batch,
ProjectStore transaction, and removal of only the committed prefix. The flaw
is the decision clock, not a missing event or a writer refusal.

Three independent primary references checked today:

- [Apache Beam timers](https://beam.apache.org/documentation/programming-guide/#timers)
  distinguishes processing-time timers from event-time timers. Actual elapsed
  time can release a batch without another input event.
- [Lodash debounce](https://lodash.com/docs/4.18.1#debounce) retains the last
  arguments for delayed invocation and offers an explicit flush. A new call
  is not needed to complete the previous trailing invocation.
- [MDN debounce](https://developer.mozilla.org/en-US/docs/Glossary/Debounce)
  describes the trailing edge after a quiet interval. Silence completes the
  wait; it is not a reason to wait forever.

Choose an explicit processing-time observation in the existing maintenance
drain. Pass it into the existing planner; retain event time for the checkpoint
timestamp, source evidence, and batch identity. Live event replay keeps its
event-time ordering. Already sealed in-flight batches retain their exact plan.
The existing debounce interval and ownership checks remain unchanged.

Rejected alternatives: forced unconditional flushing would bypass the interval;
inventing a lifecycle event would falsify provenance; rewriting event timestamps
would change identity; a new timer daemon/database is unnecessary because the
scheduled recovery path already visits durable pending queues. No dependencies,
runtime paths, configuration keys, storage formats, or limits are added.

The compromise is scheduling latency: an idle batch commits when existing
maintenance next runs after it is due, rather than at an exact timer instant.
Tests must distinguish before/at expiry, preserve source time, cover a saved
queue after restart, and prove a repeated drain does not append twice.
