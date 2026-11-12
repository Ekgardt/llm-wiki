# A full regression needs a quiescent test vault

Date: 2026-10-09. Root commit 92b356e4 completed 12,508 passing tests, 229 skips, six failures and one teardown error in 2,749 seconds. This was a failed run, not qualification.

The unchanged six failed test bodies all passed in 4.58 seconds in a clean separate checkout of the same commit. Root-specific evidence explains the difference: the doctor unit helper did not stub adoption and therefore spent its deadline examining live operational state; two MCP helper tests and a corrupt-generation fallback read the live source tree under a temporary runtime; a nightly test redirected state files but read live receipts whose committed transaction authority was absent from that temporary runtime. The fail-closed receipt check and source deadlines must stay intact. Generation was already the last doctor check; changing product order would address the wrong cause.

The publication check also encountered a generic technical word matching a current private project slug. Three prose passages now use equivalent generic wording. Its original assertion passes against the actual private project inventory; no allowlist or guard changed.

The teardown guard correctly observed changing knowledge files. An investigator checkpoint was mistakenly appended during the test session, and independently active host hooks wrote raw session evidence. Neither is permission to suppress the guard or discard captured evidence. Future full regression runs use a clean code checkout, with no private checkpoint writes during testing. Installed-runtime compilation, retrieval, health and the publication assertion against the actual private inventory remain separate required checks. Passing the isolated suite does not establish installed product completion.

## Sources and choice

The current primary sources consulted on 2026-10-09 are pytest's [temporary-directory guidance](https://docs.pytest.org/en/stable/how-to/tmp_path.html), Google's [Bazel hermeticity guidance](https://bazel.build/basics/hermeticity), and Microsoft's [unit-testing guidance](https://learn.microsoft.com/en-us/dotnet/core/testing/unit-testing-best-practices). They support separating test-owned state from external mutable state and retaining reproducible source identity. No Bazel or .NET dependency is introduced.

Alternatives rejected: weaken the no-write guard; import live transaction receipts into synthetic databases; raise source deadlines; change the already-correct doctor order; or rename private projects. Aligning individual fixtures with test-owned roots remains a possible improvement, but the unchanged isolated rerun must first distinguish product failures from fixture contamination. A clean checkout preserves every existing assertion and has the least implementation impact. Its limitation is that installed-vault behavior still requires separate real checks.

Evidence: original full-run log; unchanged six-test isolated log; source of `tests/conftest.py`, `tests/test_doctor.py`, `tests/test_mcp_server.py`, `tests/test_scheduled_nightly.py`, `tests/test_search_ranking.py`, and the unchanged publication assertion. Runtime logs remain private.
