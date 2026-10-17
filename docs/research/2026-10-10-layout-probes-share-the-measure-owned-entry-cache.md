# Layout probes share the measure-owned entry cache

Research date: 2026-10-10. Compatibility floor: Python 3.10.

## Observed behavior

A live full compiler stack entered `_uncached_part_entry_ids` through the
layout precheck used by optional-context selection. The implementation already
memoized immutable entry overlap inside a measure's existing choice-resolution
scope. Three sizing entry points tested the layout before entering that scope,
so those tests rescanned the entry table on repeated calls.

This is an identified source of redundant planning work, not an explanation of
every slow phase or of the earlier global OOM. Another stack was in DLP redaction
and a later one in receipt validation. The actual v4 receipt inventory was only
about 36 MB; that observation does not prove a multi-gigabyte receipt catalog.

## Research and choice

Three independent primary sources checked on the research date:

- [Python 3.10's functools documentation](https://docs.python.org/3.10/library/functools.html)
  describes memoization of expensive pure computations and the references a cache
  keeps to its inputs and results.
- [OpenJDK's IdentityHashMap contract](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/IdentityHashMap.html)
  distinguishes object identity from equality of values.
- [Guava's cache documentation](https://github.com/google/guava/wiki/CachesExplained)
  explains cache lifetime and the identity semantics of weak keys.

The Java sources inform identity and lifetime reasoning; no Java dependency is
introduced. Python 3.10's existing ContextVar-based scope remains the mechanism.
The chosen change puts each sizing layout probe inside the same measure-owned
scope that the subsequent serialization already uses. The existing entry cache
holds each immutable part beside its identity key, preventing identity recycling.

A global cache would outlive its measurement and risk accepting stale metadata.
A second cache would duplicate the existing mechanism. Limiting context pages
would change quality rather than remove redundant work. None is introduced.
Normal source binding, final serialization, source presence, original hashes,
complete quotes and DLP policy checks retain their independent validation.

The extra scope entry can initialize the measure's resolver on the first probe
even when no choice layout is needed. The resolver is retained only by that
measure. Scope tokens restore the previous caller state on exit, including errors.
There is no new runtime directory, schema, dependency, global cache or numeric cap.

## Qualification and limits

Three causal tests exercise byte sizing, context selection and token sizing with
real temporary source bytes. They count calls to the real uncached overlap
computation, not all entry-table iterations: some other iterations are required
validation. All three fail on the original code and pass on the candidate. A
normal caller outside sizing still recomputes overlap rather than inheriting a
sizing approval. The existing changed-hash, changed-policy, complete serialization
and partition-binding checks remain enabled.

The candidate passes 178 related checks, plus five entry-overlap checks on Python
3.10. Every new or changed function, including nested test instrumentation, was
measured with Lizard and AST inspection: maximum CCN 4, at most two `if`
statements, branching and loop nesting at most two.

A metadata-only paired measurement used a representative prefix of 13 real parts
from the immutable 11,103,934-byte October 5 daily source. It is not a
reconstruction of the first observed batch manifest. The repeat count, 2,484,
came from the current optional-note inventory. Repeated layout probes took
9.466 seconds / 9.158 CPU seconds before and 0.054 seconds / 0.053 CPU seconds
after; all Boolean results and their digest matched. Peak measurement RSS was
488,356 KiB. No production data was changed and no model was called by this probe.
These measurements do not establish full-packing, full-compilation, token or
whole-cycle savings.

Installation is deferred until the already-running full compiler finishes, so
this local optimization does not cause another costly restart. Installed
qualification, current CI and final native-index refresh remain pending. No old
implementation is kept alongside the candidate's existing cache mechanism.
