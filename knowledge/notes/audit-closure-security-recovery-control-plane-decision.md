---
type: decision
title: "Audit Closure Uses One Secure Recovery and Control Plane"
description: "Audit closure retires the unsupported Cognee bridge and adds one fail-closed DLP, recovery, install, scheduling, coordination, and evidence contract without another daemon, database, or MCP tool."
date: 2026-08-15
confidence: high
source_authority: user
status: active
---
# Audit Closure Uses One Secure Recovery and Control Plane

One-sentence summary: Audit closure retires the unsupported Cognee bridge and adds one fail-closed DLP, recovery, install, scheduling, coordination, and evidence contract without another daemon, database, or MCP tool.

## Decision

Date: 2026-08-15.

The remaining audit work will extend the existing Reliability v3 and Evidence Graph
boundaries rather than create a parallel product surface.

- Cognee is retired from the supported product. Its current low-level
  `add -> cognify -> search` bridge uses upstream legacy operations, has no main
  retrieval integration, and cannot be given a stable, secure cross-platform pin
  from the currently published releases. Existing `cache/cognee/` data remains a
  disposable legacy cache and is never deleted automatically.
- Every first-party model call passes through one fail-closed DLP boundary before
  transport. Provider output is checked before a durable digest is calculated and
  checked again from the exact bytes immediately before publication.
- Built-in detectors are always active. Optional operator literals and fingerprint
  allowlists come from the external absolute path in `LLM_WIKI_DLP_POLICY`; custom
  executable regular expressions are not accepted. A required policy that is
  missing, unreadable, invalid, or digest-mismatched blocks protected work.
- Local-only execution permits only a literal loopback Ollama endpoint and a local
  model. Cloud candidates and fallback are disabled. The product reports local-only
  as verified only when Ollama cloud disablement is also verifiable; otherwise it
  reports the external-runtime limitation instead of claiming isolation.
- Restic is the supported encrypted private-vault backup engine. Backup uses the
  existing maintenance fence, SQLite online backup, immutable file/hash manifests,
  and a temporary staged recovery image. Live, unknown, ambiguous, raced, corrupt,
  or schema-invalid state blocks backup. Restore validates into an empty staging
  target before publication and never guesses historical ownership.
- `run/install/` is the only new runtime directory. It contains resumable install
  state, owned external-path manifests, verified preimages, scheduler definitions,
  and non-secret Restic configuration. Secrets remain in an external password
  command or protected file supported by Restic.
- Windows uses Task Scheduler, macOS uses a per-user LaunchAgent, and Linux uses a
  per-user systemd timer. Cron is an explicit degraded fallback, not the preferred
  Unix path. Install, upgrade, rollback, restore, and uninstall mutate only
  manifest-owned paths and do not perform automatic Git operations.
- Blackboard claims use fenced tables in the existing transaction database. Capture
  producers use existing Queue v3 intents and terminal proof. No new operational
  database, daemon, runtime root, or MCP tool is introduced.
- Grounded QA defaults to extractive verified spans. Ranking remains relevance-first
  with a bounded authority/confidence tie-break. Agent attribution covers OpenCode,
  Codex, Claude, Cursor, and Antigravity. Loop detection reports single-agent churn,
  multi-agent loops, and recurring normalized errors separately.
- Maintenance output is streamed under byte and time limits. Exceeding a limit stops
  the producer, so the retained protected artifact is complete for the permitted
  execution. One age/count/size policy covers maintenance reports.

## Compatibility and paths

The three-zone root layout, `LLM_WIKI_ROOT`, `LLM_WIKI_STATE_ROOT`, Markdown
authority, two Reliability v3 operational databases, and the 12-tool MCP surface do
not change. The approved additions are:

- `run/install/` under the existing runtime root;
- optional external `LLM_WIKI_DLP_POLICY`;
- manifest-owned user LaunchAgent/systemd files and existing Windows scheduled
  tasks;
- Restic repositories that must be outside the vault and staging tree.

Retired Cognee files are not migration authority. They may be removed manually after
the operator no longer needs them, but installation and repair never remove them.

## Rationale

The repository already provides generation-consistent FTS, vectors, graph evidence,
and native code navigation. Keeping a second graph stack adds dependencies and
failure modes without serving the main search path. Current Cognee documentation
marks the bridge operations as legacy; its stable PyPI release is 1.4.2 while the
current development line contains security, packaging, migration, and process-model
changes. Retiring that bridge is smaller and safer than adopting an unstable
compatibility layer.

Restic was selected over Kopia and Borg for this product boundary because it offers
official Windows, macOS, and Linux binaries, authenticated encryption, repository
checks, restore, and a single-process CLI without a required daemon. The LLM Wiki
wrapper still owns coherent application snapshots because a file backup utility
cannot make independently changing Markdown and SQLite state logically atomic.

## Rejected alternatives

- Pinning Cognee 1.4.2 was rejected because it preserves legacy APIs and misses
  fixes already present only in the development line.
- Adopting Cognee 1.5.0.dev2 was rejected because it is a development pre-release
  with migration, dependency, macOS installation, and single-process constraints.
- Kopia was not selected because its policy, maintenance, GUI, and optional server
  surface is broader than this no-daemon wrapper needs.
- Borg was not selected because native Windows is not a supported production path.
- Directly backing up live SQLite files was rejected because it cannot prove one
  coherent recovery point.
- User-supplied DLP regular expressions were rejected because unbounded regex
  execution would create a denial-of-service boundary.

## Acceptance

Implementation proceeds test-first. Completion requires transport-level secret
fixtures, local-only negative tests, multiprocess fencing tests, interrupted install
and recovery tests, encrypted clean-target restore with hash and database checks,
native scheduler verification, adversarial irrelevant-citation tests, bounded-output
tests, full local regression, and exact-commit remote CI. External evidence remains
pending until the real providers, operating systems, agents, and backup target have
actually been exercised.

## Source / Evidence

- Explicit user approval in the 2026-08-15 OpenCode session.
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-14.md`.
- Cognee Python API: https://docs.cognee.ai/python-api.md
- Cognee changelog: https://docs.cognee.ai/changelog
- Cognee local setup: https://docs.cognee.ai/guides/local-setup.md
- Cognee security: https://docs.cognee.ai/setup-configuration/security.md
- Cognee PyPI stable release: https://pypi.org/project/cognee/1.4.2/
- Ollama FAQ: https://docs.ollama.com/faq
- Restic installation: https://restic.readthedocs.io/en/stable/020_installation.html
- Restic repository checks: https://restic.readthedocs.io/en/stable/045_working_with_repos.html
- Restic restore: https://restic.readthedocs.io/en/stable/050_restore.html
- Kopia features: https://kopia.io/docs/features/
- BorgBackup: https://www.borgbackup.org/
- systemd timers: https://man7.org/linux/man-pages/man5/systemd.timer.5.html
- Apple launchd: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- OWASP LLM02:2025: https://genai.owasp.org/llmrisk/llm022025-sensitive-information-disclosure/

## Related
- [[knowledge/notes/secret-shape-not-secret-name-decision]] — what the redactor may treat as a secret: the value decides, not the name beside it.

- [[solo-operator-superset-product-decision]]
- [[v4-reliability-contracts-decision]]
- [[reliability-v3-runtime-adoption-implementation-decision]]
- [[derived-evidence-generation-decision]]
- [[integration-config-backup-retention-decision]]
- [[knowledge/notes/blackboard-fenced-resource-claims-decision]]
- [[knowledge/notes/install-ownership-control-plane-decision]]
- [[knowledge/notes/managed-ide-hooks-install-update-decision]]
- [[knowledge/notes/baseline-environment-binding-decision]]
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]]
- [[knowledge/notes/system-symlink-ancestor-decision]]
