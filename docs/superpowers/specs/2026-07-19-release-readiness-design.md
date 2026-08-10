# Release Readiness Design

## Goal

Produce a clean release-candidate branch that is ready for a public GitHub review and has no known runtime defects. Readiness means all available automated checks and real pinned-model runs pass. It does not mean claiming that future bugs are impossible or publishing model-superiority claims without complete evidence.

## Release Boundary

The release candidate is `one-day/integration`. Markdown, Git, and project journals remain authoritative. Model caches, benchmark outputs, graph generations, logs, and coordination databases remain runtime or external evidence and are not committed.

Real model execution is a release gate. Every pinned embedding and reranker candidate must be acquired into an operator-selected cache outside the repository, then exercised offline against the frozen EN/RU/ZH corpus. Acquisition receipts are not quality evidence. Only offline reports with exact model revisions, complete provenance, and no semantic fallback qualify as model evidence.

Real Graphify comparison remains a research claim gate, not a runtime release gate. The public documentation must continue to say `evidence pending` unless the pinned comparison protocol completes. The product must not depend on Graphify or gated model availability at runtime.

## Components

### Model Matrix

`benchmark/run_retrieval_v2.py` acquires and runs the frozen candidates from `benchmark/model-matrix-v1.json`. Each candidate runs in a bounded worker with an explicit cache root and deadline. The release process verifies EN/RU/ZH behavior, model loading, resource measurements, offline reuse, and truthful fallback reporting.

The BGE-M3 sparse projection must load the exact pinned `sparse_linear.pt` state dictionary, including both weight and bias. Hash, revision, shape, cache containment, and remote-code restrictions remain fail-closed.

### Runtime Hardening

The release process exercises MCP, doctor, sync, install contracts, Evidence Graph generation health, retrieval security, structure, and multilingual README synchronization. Any reproducible defect receives a failing regression test before the smallest implementation fix.

### Legacy Policy

Only code proven unused by tracked callers, tests, installers, documentation, and migration contracts may be removed. Legacy cache readers remain read-only compatibility paths until installed-vault migration evidence proves removal safe. No legacy writer or legacy path becomes an active default.

### Evidence And Publication

Raw model and scale reports are written to an operator-selected temporary directory outside the repository. Public documentation reports only verified results. Missing candidates, unavailable platform checks, or incomplete comparative evidence remain explicit and cannot be converted into success claims.

## Error Handling

Model download is separated from offline evaluation. Authentication, license, network, checksum, revision, dependency, timeout, memory, and schema failures remain distinct. A candidate failure produces a bounded diagnostic or degraded report; it never silently falls back and then claims model quality.

Repository mutations stop if unexpected user changes overlap the release work. No stash, reset, force checkout, force push, or destructive worktree cleanup is allowed.

## Verification

Release readiness requires:

- real offline execution of every pinned embedding and reranker candidate;
- frozen EN/RU/ZH corpus and exact revision provenance;
- regression tests for every defect found during real runs;
- `uv run ruff check scripts/ tests/ benchmark/`;
- `uv run pytest -q`;
- `uv run pytest tests/test_readme_i18n.py tests/test_structure.py -q`;
- `git diff --check` and a final worktree review;
- available lint, doctor, MCP, benchmark, security, and scale smoke gates;
- documentation that separates shipped runtime behavior from pending research evidence.

Windows verification is local. POSIX-only checks require Linux CI and remain a visible external gate until CI reports them green.

## Completion State

The branch is ready for public GitHub review when all release gates that can run in the current environment are green, real model evidence is retained externally, no known defect remains open, and every unavailable external gate is accurately documented. Push, pull request creation, and merge occur only after explicit user instruction.
