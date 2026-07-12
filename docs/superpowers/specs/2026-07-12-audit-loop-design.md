# Audit Loop Design

## Status

Approved design on 2026-07-12. Implementation requires a separate approved
plan. This document defines the target process; it does not claim that the
process already exists.

## Goal

Prevent the recurring pattern of fixing a reported defect while leaving the
same defect class available elsewhere. The loop must make the complete audit
scope visible, require reproducible evidence for every result, and block a
merge or release until verified quality thresholds are met.

The process provides a process guarantee, not a claim that every unknown bug
can be discovered: known defect classes become enforced invariants, every
declared file is reviewed or explicitly excluded, and unresolved uncertainty
cannot be closed as success.

## Scope And Exit Criteria

Each audit starts from a recorded commit SHA and a clean audit baseline. Its
manifest covers every tracked text or configuration file, including root
metadata and the following zones. A tracked binary requires an explicit
`excluded` entry that records why it cannot be reviewed as text.

- Code: scripts, tests, installers, CI, pre-commit, benchmark, and pyproject.
- Knowledge: notes, public fixtures, index, log, and workflow pages.
- Documentation: all README translations, root docs, docs, skills, rules, and
  integrations.

Every manifest entry is `reviewed`, `excluded` with a reason, or `failed`.
An audit cannot complete with an uncovered entry.

The only successful exit is:

- 0 Critical findings.
- 0 High findings.
- 0 Medium findings.
- At most 2 Low findings.
- 0 unverified findings.
- All local and CI gates pass on the audited commit.
- Every confirmed Critical, High, or Medium finding has a regression
  invariant that fails before the fix and passes after it.

`[UNVERIFIED]` is always a blocker. An external API, network service, or
platform behavior must be reproduced in a controlled check or mitigated
conservatively before the finding can close.

## Lifecycle

### 1. Baseline

The audit runner records the commit SHA, repository status, tracked-file list,
and counts for scripts, tests, notes, skills, rules, integrations, and docs.
It refuses to label a result complete if unrelated working-tree changes make
the evidence ambiguous. It never modifies the public source vault while
performing a read-only audit.

### 2. Read-Only Audit

The auditor reads the canonical sources first: AGENTS.md, STRUCTURE.md,
pyproject.toml, .gitignore, test_structure.py, knowledge/index.md, and
knowledge/log.md. It then reviews each manifest entry in alphabetical order.

The audit uses only read operations, git history inspection, and one Ruff
report. It does not run pytest, memory pipelines, or mutation commands. Each
finding includes a severity, path and line, evidence, impact, affected
contract, suspected root cause, and reproducible check. The report separates
confirmed facts from hypotheses.

### 3. Triage

Triage deduplicates findings and assigns each confirmed item exactly one
defect class. Initial classes are:

- atomic-write
- lock-ownership
- path-safety
- secret-redaction
- runtime-contract
- clean-clone
- docs-sync
- i18n-parity
- release-integrity
- workflow-supply-chain
- OKF-validity
- link-integrity

Severity follows impact and exploitability, never the convenience of a fix.
A TODO, comment, or future plan is not a mitigation and does not lower
severity.

### 4. Research And Fix

Before changing a security, CI, dependency, platform, or runtime contract,
the implementer consults current primary guidance and records the source in
the change rationale. Examples include upstream project documentation,
GitHub Actions security guidance, Python documentation, and OpenSSF guidance.

The fix addresses the root cause for the defect class. It must preserve the
three-zone and environment contracts. It must not overwrite unrelated local
changes. Architecture, path, environment, or runtime changes still require
the explicit approval process in AGENTS.md.

### 5. Regression Conversion

Every confirmed Critical, High, and Medium finding becomes an invariant with
red/green evidence: it fails against the vulnerable behavior and passes after
the fix. The test must cover the defect class across all applicable paths,
rather than asserting a particular source line or implementation detail. Use
behavioral tests for concurrency, persistence, and security boundaries; use
static guards for deterministic repository-wide rules such as docs parity,
path names, action pins, and frontmatter.

Low findings receive a guard when their class can be checked reliably. A Low
without an automated guard remains visible and counts toward the two-item
exit budget.

### 6. Verification And Independent Re-Audit

Verification occurs in this order:

1. Run the targeted invariant for the change.
2. Run the complete local quality gate.
3. Run a clean-clone import and installer-contract check.
4. Run an independent read-only audit with a fresh context that has not seen
   the original findings.

If any gate fails, if a re-audit finds a Critical, High, or Medium item, if it
finds more than two Low items, or if anything remains unverified, return to
triage. Do not report success early.

### 7. Closure And Release

The closure record contains the audited SHA, manifest coverage, findings by
class and severity, the invariant introduced for each relevant finding, and
the exact verification evidence. Merge and release are allowed only after the
CI gate passes for that SHA.

## Enforcement

### One Canonical Gate

The project will define one canonical local command. CI invokes the same
command rather than a handwritten approximation. It includes:

- Ruff over scripts and tests.
- The full pytest suite.
- Structure, public-source, clean-clone, docs/i18n, and security-invariant
  tests.
- Deterministic checks for OKF, wikilinks, Markdown paths, script references,
  versions, test-count claims, and README/install parity.
- Secret scanning.
- A tracked-file audit-manifest coverage check.

The audit itself remains read-only and does not run the full gate; the gate is
the post-fix verification phase.

### CI Security

GitHub Actions use least-privilege permissions and third-party actions pinned
to verified full commit SHAs. Action pins are checked as an invariant.
Dependabot updates pinned references with the release tag retained in a
comment. Workflow inputs are treated as untrusted and passed through
environment variables or validated arguments, not interpolated into shell.

### Finding Ledger

The implementation will maintain an append-only, tracked finding ledger. It
will connect a finding identifier to its defect class, root cause, invariant,
fix commit, verification command, and closure SHA. The ledger is not a TODO
list: open findings fail the relevant gate; closed findings retain evidence.

## Failure Handling

- A false positive is closed only with evidence explaining why the invariant
  does not apply. Its exception is narrow, documented, and tested.
- A non-deterministic failure blocks closure until it has a deterministic
  reproducer or a conservative failure mode.
- An unavailable external dependency blocks closure when it is required to
  verify a security or contract claim.
- A failing invariant introduced during the cycle takes precedence over a
  passing full-suite summary.

## Implementation Boundaries

The implementation must not create a second test, lint, or release policy.
It extends the existing quality-guard and security-invariant suites, shares
existing path and state helpers, and preserves the public-source versus
installed-vault boundary. Exact files, commands, and CI job names are left to
the implementation plan so they can be verified against the current tree.

## Research Basis

- GitHub, "Secure use reference": full commit-SHA action pins, least
  privilege tokens, untrusted-input handling, Dependabot, and code scanning.
  https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
- GitHub, "About code scanning": scheduled and event-driven static analysis.
  https://docs.github.com/en/code-security/code-scanning/introduction-to-code-scanning/about-code-scanning
- OpenSSF Best Practices Criteria: automated CI, regression tests for fixes,
  static analysis, current documentation, and dependency monitoring.
  https://www.bestpractices.dev/en/criteria
