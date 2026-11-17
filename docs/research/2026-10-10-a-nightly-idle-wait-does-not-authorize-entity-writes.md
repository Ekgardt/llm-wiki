# An exhausted nightly wait does not authorize entity writes

The installed nightly pass waited for an already running compiler, exhausted
its existing idle wait, and continued into fact-key extraction anyway. That step
extended entity pages which were already in the compiler's immutable target
snapshot. The compiler correctly refused publication of a plan based on the old
bytes. Ordering fact keys before the night's own compile did not protect a
compiler that was already running under another owner.

The correction checks compiler state again after the idle wait. If it is still
running, the nightly records the existing deferred outcome and returns the
failures already observed during intake. It runs neither entity writes nor its
own compile step. A status exception now propagates rather than being converted
to a false claim that the compiler finished. If the compiler finished during the
wait, the existing fact-keys-before-compile order remains unchanged.

This adds no path, configuration, ownership protocol, runtime root or numerical
limit. The existing wait remains a scheduling allowance, not mutation authority.
The observation does not establish exclusive ownership: an independent writer
or compiler may act afterwards. The compiler's existing hash/CAS refusal still
requires a fresh model plan for changed target bytes; it is not weakened.

## Research checked on 2026-10-10

- [Python 3.10 time.sleep](https://docs.python.org/3.10/library/time.html#time.sleep)
  describes suspending a caller; elapsed sleep does not observe another process's
  completion. Python 3.10 remains the compatibility floor.
- [Linux clock_nanosleep](https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html)
  describes timer-based suspension and interruption, distinct from establishing
  an application condition. No Linux-only primitive is added.
- [Microsoft synchronization](https://learn.microsoft.com/en-us/windows/win32/sync/using-synchronization)
  describes synchronization through the appropriate condition or object. The
  existing shared compiler-status check and publication preconditions retain
  the cross-platform contract.

Chosen alternative: defer the conflicting entity stage using existing state and
the existing deferred record. Increasing the sleep was rejected because a
compiler can outlast it. Removing target checks was rejected because it permits
stale model plans. A new long-held global writer lock around model calls would
block unrelated capture and is unnecessary for this scheduling correction.

## Evidence and completion boundary

The live nightly log records the exhausted wait followed by entity updates.
The transaction record binds the changed target's before and after hashes, and
the compiler log records its corresponding snapshot refusal. The work and its
accepted receipts were retained; the interrupted compile is not a full-cycle
qualification or a token-saving result.

Three causal tests failed before the correction: a running compiler's target
was changed, unreadable status allowed writes, and intake failure did not prevent
the conflicting entity stage. The finished-during-wait case already passed.
The corrected related tests preserve both the deferred unknown outcome and
existing errors. The original independent-target-edit test still refuses the
old model plan. Installation, the resumed full compile, generation, retrieval,
full nightly health and CI remain separate completion gates.

Source: `scripts/scheduled_nightly.py`; `scripts/maintenance_helpers.py`;
`tests/test_nightly_busy_compile_keeps_entity_targets_unchanged.py`;
`tests/test_nightly_fact_keys_precede_compile_snapshot.py`; private causal
timeline, transaction identities, compiler-cost report and related test logs.
