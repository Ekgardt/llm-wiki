# Generation windows follow the measured vault

On 2026-10-08 the installed vault selected 107,490 sources and 224,426 chunks.
Collection alone took 260.364633 seconds. The hidden 120-second post-compile
window could not complete collection; the nightly 900-second window also deferred.
A measured 1812.724293-second attempt wrote all six artifacts but expired during
validation. A complete independent validation of the retained parent took
52.784075 seconds. A subsequent attempt reserved the previous full window,
validation scaled by observed source/chunk growth, and one measured collection
window for variation: 2131 seconds. It was killed by the kernel OOM killer at
14:05:38 UTC, before activation. **There is no successful installed full-build
qualification yet.** Increasing time alone does not fix memory pressure.

The owner delegated the decision in response to the explicit proposal for
`generation.nightly_seconds` and `generation.post_compile_seconds`. The private
immutable decision and `docs/STRUCTURE.md` were written before implementation.
The two windows now use the existing strict integer settings registry, including
file/environment precedence, explicit invalid-value errors and live rereads.
2131 seconds is the measured estimate above, not a universal performance claim.
Operators should remeasure after corpus growth or machine changes; shorter windows
must still complete the required artifacts and validation. Sources, context,
vectors, citation guards and catalog CAS activation are unchanged.

The night's aggregate bound uses the same effective generation setting. Linux
service and Windows task definitions retain the existing four/six-hour floors and
increase to the configured complete pass plus the existing startup margin, rounded
up to whole hours. Windows definitions keep their saved positive integer hours:
rollback/status replay those exact values even after current settings change.
Version 1/2 records remain readable. The replaced static systemd table is removed;
doctor checks the configured vault's expected definition and names a required
installer refresh after a window change. Existing historical floor tables remain
for default scheduling and legacy compatibility. macOS and explicit cron have no
new external kill timeout.

Five causal tests failed on the original release and passed on the candidate.
Additional guards exercise actual PowerShell registration with supplied hours,
invalid saved hour values, and doctor checking a vault different from the process
root. Python 3.10 related qualification: 194 passed, including the native PowerShell
7.6.6 parser and registration stubs on Linux. This is not native Windows Task
Scheduler qualification or the full product regression suite. The analyzer measured
39 changed/new Python callables with CCN <= 5, at most two ifs and nesting <= 2.
Native PowerShell AST measured the two changed functions: CCN 3 and 1, at most two
ifs, nesting <= 1. Ruff and diff checks passed. Two test-fixture mistakes during
qualification were corrected rather than weakening checks: uncanonical JSON never
reached hour validation, and a first test command used the wrong checkout.

Memory diagnosis and cleanup are separate evidence. The kernel killed PID3016543;
the last observed generation RSS was about 3 GiB. The resident retrieval MCP used
about 2 GiB, RAM-backed scratch about 5.8 GiB, and another code-index worker was
present. Those observations establish system memory pressure, not an allocation
attribution to every byte. Eight exact unowned scratch roots were qualified through
all-process fd/cwd/maps scans. Completed synthetic fixtures were archived and every
regular archived file hash verified; six fixture trees and two derived vector read
copies released 1,259,892,736 original allocated RAM bytes. The verified fixture
archive occupies 49,807,360 disk bytes. Authoritative memory, production `run/`,
active/fallback generations and original failure reports remain. A first ZIP attempt
rejected fixture timestamps before 1980; original timestamps remain in the archive
inventory. A later removal stopped on readonly BagIt fixtures; resumed removal
verified every surviving entry against the archived inventory before using elevated
permissions on the exact qualified roots. Neither failed attempt is a success.

Research checked 2026-10-08, three independent primary sources:

- [Python 3.10 monotonic time](https://docs.python.org/3.10/library/time.html#time.monotonic): elapsed deadlines do not depend on wall-clock adjustment.
- [gRPC deadlines](https://grpc.io/docs/guides/deadlines/): choose deadlines from measured work and propagate cancellation between components.
- [Microsoft cooperative cancellation](https://learn.microsoft.com/en-us/dotnet/standard/threading/cancellation-in-managed-threads): the operation observes cancellation and releases its own resources.

The existing Python 3.10-compatible builder, fenced maintenance, cancellation and
SQLite generation publication stay in use; no gRPC/.NET runtime dependency is added.
Unbounded waits, unexplained larger constants, source/context truncation and skipping
vectors/validation were rejected. A full useful compile with complete token usage,
quality assessment, current native lifecycle evidence and a green full regression
suite remains required to close audit point 7.

Evidence: `logs/audit-2026-10-08-step7-current-corpus-budget-profile.json`,
`logs/step7-installed-memory-generation-refresh-20261008-measured-budget.json`,
`logs/step7-complete-parent-generation-validation-cost-20261008.json`,
`logs/step7-installed-memory-generation-refresh-20261008-oom-outcome.json`,
`logs/step7-generation-full-budget-kernel-oom-20261008.txt`,
`logs/step7-generation-time-budgets-decision-20261008.json`,
`logs/step7-qualified-completed-fixture-ram-retirement-resumed-20261008.json`;
qualification artifacts under `/dev/shm/step7-generation-windows-*20261008*`.

Additional doctor/generation qualification: 378 passed, 6 existing platform/permission skips. Normal fenced `GenerationCatalog.discard_unactivated` retired only the OOM candidate after a complete ownership scan (zero references/unreadables), saved artifact hashes and proved the active generation unchanged. It released 1,376,520,320 logical derived bytes. Evidence: `logs/step7-qualified-oom-candidate-retirement-20261008.json`. Full regression remains open.
