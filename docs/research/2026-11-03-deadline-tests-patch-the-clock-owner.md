# Deadline tests replace the clock in its actual owner

2026-11-03. Full pinned 446bdafa regression finished: 15370 passed, 244 skipped,
1 failed, 5 warnings, 1790.47 seconds. This was not a full pass. The nonexpired
SQLite interrupt case reproduced on 1d7cb29c: real monotonic time was used after
the test replaced reliable_memory.time with a private fake namespace.

The deadline functions moved with unchanged AST to sqlite_deadlines. Importing the
same stdlib time object preserves attribute patching, but replacing an entire
module binding only affects that binding. Initial consumer research checked the
former and missed three replacements of the latter in test_a_check_names_its_cause.
An alias-aware AST scan inspected 189 test files mentioning reliable_memory; the
three whole-clock replacements were all in that file. No production clock lookup
or deadline behavior is changed by this correction.

The three tests now replace sqlite_deadlines.time. Parameter sets, exception types
and original error-identity assertions remain intact. The certification test is
also strengthened: it must reach the injected query and retain a SQLite exception
as timeout context. Previously an early timeout could pass without exercising SQL
interruption. This is test instrumentation repair, not suppression of a runtime
error, and no shared-global forwarding shim or guard exception is introduced.

Fresh graph at 1d7cb29c included the SQLite error normalizer's callers; source and
clock patch sites were inspected. Reproduction: 1 failed, 11 passed. Corrected
cause/deadline/compatibility tests: 27 passed. Evidence: logs/sqlite-clock-owner-
{red,green}.txt, logs/sqlite-clock-owner-rebindings.json, and pinned full-run logs.
The complete regression remains required on a later candidate. No live rollout.

The full cause test file also passes on Python 3.10: 12 passed. Its measured
weight run passes all 12 cases; local CCN/shape and Ruff pass. The assertions now
prove the certification callback was reached before the timeout, rather than
accepting an earlier unrelated timeout.
