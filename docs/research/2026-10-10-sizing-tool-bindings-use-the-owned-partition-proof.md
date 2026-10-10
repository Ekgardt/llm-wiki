# Sizing joined tool evidence uses the partition proof it already owns

A real closed-day compile spent 1151.70 seconds before making a model call;
1143.14 seconds were process CPU. A sampling trace reached ordinary evidence
binding inside source-choice sizing, then `_require_native_unit`,
`_join_partition`, and `_native_partition_ranges`. Joining a captured tool line
reparsed the immutable day's records despite the sizing pass already owning
their exact partition proof. Two bindings of the existing split-tool fixture
reparsed the source four times. The causal test fails on the previous code.

The existing `_SOURCE_CHOICE_PARSING` context already carries the sizing pass's
`_DailyPartitionProofs`. The implicit join now uses that canonical parser when
its exact type is present. It retains strongly referenced immutable source bytes
and keys their partition by object identity and the original digest. An explicit
partition parser follows its existing path. Without the sizing context, or with
an arbitrary object in it, the original full parser runs.

This reuses partition metadata, not an evidence verdict. Every join still checks
source identity, original bytes, partition ordinals, bounds, member bytes and
hashes, contiguity, and complete native/tool-line coverage. Changed member bytes,
digest, ordinal, or original source fail. After the context ends, final evidence
binding reparses. Protected layouts still apply the current DLP policy; receipt,
transaction, source-read, and publication checks are unchanged. There is no new
cache, limit, dependency, path, environment variable, or runtime contract.

## Qualification

Eight tests cover the repeated parse, unchanged exact citation and quote digest,
final fresh parsing, forged members, replaced original bytes, and a foreign
context object. The old code gives one failure and seven passes. The correction
passes those tests and all 706 compile/native tests. The related Python 3.10
qualification passes 37 tests. Actual AST/Lizard analysis of all eight new or
changed callables gives maximum CCN 2, at most two `if` statements and at most two
levels of nesting. The new test file's shard weight is measured by the unchanged
official shard planner.

A paired measurement binds the same ordinary row in a real joined tool unit,
retaining the same 11,104,218 source bytes. Two bindings take 0.927 seconds and
two source reparses before the correction, versus 0.268 seconds and no reparses
with the already-owned sizing proof. Both produce exactly the same bindings,
including reference and digest bytes. Source SHA-256:
`045cf832b4f7b421fe111e1707de3d6306d2ab103b69b68ac41c9a841220febe`.
This is a paired binding measurement, not a completed compile, token saving,
retrieval qualification, or product-completion claim. The original and corrected
measurement reports retain the instrumentation limitations of earlier probes.

## Research and alternatives — 2026-10-10

Python 3.10 is the supported floor and was tested. No library version changes.

- [PSF Context Variables documentation](https://docs.python.org/3.10/library/contextvars.html)
  describes operation-local state and token-based restoration. Reuse stays within
  the product's existing context rather than introducing global retained state.
- [SQLite isolation](https://www.sqlite.org/isolation.html) distinguishes committed
  operational evidence from in-process derived metadata. Reusing a partition
  does not cache a database or publication approval.
- [OWASP Input Validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html)
  supports retaining semantic checks of the actual input. Cached boundaries do
  not replace the member-content and digest checks.

Reparsing the entire immutable source for every quotation preserves correctness
but repeats CPU work. A global cache would introduce another lifetime and invalidation
contract. Increasing a model window or deadline would not remove the observed
CPU work. Reusing the existing owned parser is chosen because it changes only
the missing connection and keeps the final authority boundary independent.

The original parser remains necessary outside sizing and for unsupported context
objects. It is the final validation path, not a parallel retired implementation.
