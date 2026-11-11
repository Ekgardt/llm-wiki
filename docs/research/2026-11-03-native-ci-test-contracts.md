# Native CI exposed two test-contract assumptions

Date: 2026-11-03. These are regressions in qualification, not evidence of a runtime
serialization change. Actions run 41609824640 supplied the initial evidence.

The source-inspection relocation preserved test function bodies but missed two
external users of module-level NAMES in queue_timing and windows_acl boundaries.
Existing test_memory_queue and test_runtime_publication_verifies_instead_of_rehardening
failed with ImportError on Linux and macOS. Both fail locally before correction.
Restore the exact original constants at those already-consumed pure-test paths;
the relocated integration checks import them instead of keeping duplicate sets.
Existing consumer assertions stay unchanged and now run as direct regression gates.
A function-only AST transfer proof cannot establish compatibility of module exports.

Worker/prefix pickle byte fixtures were captured with protocol 4. Python 3.14
changed pickle's default to 5, causing nine byte comparisons to fail although the
public classes/functions retain identity. Compare the old bytes with explicit
protocol 4 and also test protocol-4, protocol-5 and default round trips. Preserve
all historical bytes and old-value deserialization assertions. No production
pickle default or stored format changes; do not replace the golden fixtures.

Primary sources checked 2026-11-03:
- https://docs.python.org/3.14/library/pickle.html#pickle.DEFAULT_PROTOCOL — the
  protocol default changed; compatible explicit protocols preserve old formats.
- https://docs.pytest.org/en/stable/explanation/goodpractices.html — test imports
  depend on import mode and layout; shared test data also has real consumers.
- https://bazel.build/basics/dependencies — data and internal artifacts are
  dependencies, not only executable function bodies. No Bazel adoption proposed.

Evidence: CI jobs 125828625939, 125828626046, 125828626059, 125828626070 and
125828626103; logs/ci-test-contracts-red.txt. Full native matrix remains pending.

Both missing imports failed locally before restoring the original constants.
The corrected compatibility set passed 139 tests on Python 3.12 and 3.10; the
expanded encoding/compatibility set passed 204 on an isolated, locked Python 3.14
environment. All changed/new functions pass actual CCN and shape checks. The full
b5cee495 regression later reported exactly the two missing imports (16,497 passed,
244 skipped); that earlier checkpoint is not falsely reported as green.
