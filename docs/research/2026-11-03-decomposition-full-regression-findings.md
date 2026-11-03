# Two transfer omissions found by the full regression

2026-11-03. The pinned candidate df42c0e2 full suite completed in 1805.54 seconds:
15297 passed, 244 skipped, 2 failed, 5 warnings. This is not a successful full run.
Both failures reproduced on dc471870 before the correction.

The child runner extraction moved CHILD_LEASE_CHECK_SECONDS but omitted its
adjacent rationale. The source before extraction retained the explanation and
reference to the lease-loss design. Restore those exact two comment lines beside
the constant; its value and executable AST remain unchanged. The existing limit
scanner detects this omission across all scripts; no exemption was added.

A schema-boundary report retained the installed checkout's absolute home path.
Replace the machine-specific location with the product name. The original private
run logs retain precise paths; public prose needs only the installed/candidate
role and commit identity. The existing tracked-text privacy sweep checks the
whole tree; no pattern or allowlist was weakened.

Relevant source graph was refreshed at dc471870 and the child waiting caller
queried before correction. The combined complete limit/privacy suites and child
module checks now pass: 113 passed, 1 skipped (clean-checkout private-project
inventory). Evidence: logs/decomposition-full-df42c0e2.{txt,xml},
logs/decomposition-full-findings-{red,green}.txt. The full suite must run again on
a later pinned candidate. No live installation or historical full-pass claim.
