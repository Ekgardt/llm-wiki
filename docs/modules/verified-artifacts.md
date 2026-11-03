# Reuse of verified artifact evidence

`scripts/verified_artifacts.py` remembers caller-established artifact digests by
existing stat identity and content verdicts by digest. It is a disposable cache,
not a source of knowledge or an independent validator. Callers still establish
truth, select generations and own open readers. Unknown, changed, racily recent
or unreadable evidence requires fresh verification; missing/corrupt cache data
costs work rather than granting trust. This contract assumes immutable registered
artifacts under the existing local-filesystem rules, not a hostile-file sandbox.

The cache schema/path, timestamp comparison, write/sync/replace sequence, entry
order, eviction and limits remain unchanged. A failed cache save does not change
knowledge or replace caller verification. No background worker or new root is
introduced. Public imports and all production source bytes remain unchanged.

The packet includes seven pure cache regressions, the two verdict-eviction tests,
and their shared timestamp setup helper. Six graph/search integrations remain
with those consumers in the original suite and import the same helper. Their
future ownership is still visible in the whole-project inventory. No assertion
is dropped and no implementation is duplicated.

Run `pytest tests/modules/verified_artifacts
 tests/test_the_verdict_cache_forgets_the_oldest.py -q`; related graph/search
suites remain required integration evidence. Source/guide coverage, stdlib-only
and isolated import checks, and the complete serialized token budget apply.
Python 3.14 is the approved runtime; native OS qualification is recorded separately.
