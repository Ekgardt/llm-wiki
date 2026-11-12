# Paired maintenance: correctness observed, full cost unavailable

Date: 2026-11-03. The owner explicitly authorized two independent subagents.
Both started from fd9918c4 in separate detached checkouts, with Python 3.14.7,
the same task and fresh built-in architecture/caller evidence. The full arm
read answer_budget.py; the contract arm read the verified interface and semantics
and reported no provider implementation read. Neither received the other's work.

The task was to exclude only reserved `data.answer_cost` from repeated answer
estimation without changing the real estimator, other content, attachment policy
or input containers. Both implementations independently produce a shallow copy
of the envelope and data dictionary before removing that reserved entry. The
full arm used dict(data); the contract arm used data.copy(). Ordinary dictionaries
behave equivalently; this task does not promise arbitrary subclass purity.

The full arm first reproduced four failing regression cases and then passed
63 owner/provider/integration tests. The contract arm directly reproduced the
changing estimate and then passed 64 tests. Both retained the real dependency
and passed actual CCN/branch-shape and Ruff checks. Root reviewed both patches
and cross-ran each implementation against the other arm's tests: 64 and 63
passed respectively. The selected correction uses dict(data) and retains the
full arm's regression cases, including invalid old telemetry, Unicode, failure
nonmutation and unchanged auto/never refusal. This defect fix is independent of
whether dependency source omission is adopted.

Baseline full packet: 81366 o200k_base tokens; candidate: 76862. These are static
artifact sizes, not model usage. Neither participant nor the orchestration
interface exposed complete model token counters or authoritative full elapsed
times. Partial command timings are preserved as partial. No full-cycle savings
or latency improvement can be established. One successful pair is limited
correctness evidence and does not qualify unrelated boundaries.

**Status: correctness observed; cost instrumentation incomplete; source omission
not adopted.** The canonical full-source gate and 100000-token ceiling remain
unchanged. Condition 6 of the approved pilot remains incomplete until an
instrumented paired task supplies actual full-cycle costs.

Retained evidence: each isolated participant's report, original patch and test
logs under `logs/paired-trial/`; root's `paired-root-cross-check.json` and
`paired-maintenance-result.json`; fresh baseline candidate qualification. A
missing counter is represented by null, never by an estimated savings number.
Related: [approved pilot](2026-11-03-qualified-dependency-contract-proposal.md).
