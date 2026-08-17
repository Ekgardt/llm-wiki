---
type: decision
title: "Install Ownership Uses One Resumable Manifest Control Plane"
description: "Profile, user environment, and native scheduler mutations use one bounded run/install manifest and resumable transaction that fail closed on ambiguous ownership or external drift."
date: 2026-08-16
confidence: high
source_authority: user
status: active
---
# Install Ownership Uses One Resumable Manifest Control Plane

One-sentence summary: Profile, user environment, and native scheduler mutations use one bounded `run/install` manifest and resumable transaction that fail closed on ambiguous ownership or external drift.

## Decision

Date: 2026-08-16.

The first install-control implementation slice adds no database, daemon, MCP tool,
runtime root, or automatic Git operation. It uses this exact runtime layout:

- `run/install/manifest.json`: the active committed ownership generation;
- `run/install/transaction.json`: the one resumable operation;
- `run/install/install.lock`: a process-lifetime advisory writer lock;
- `run/install/preimages/`: content-addressed verified owned fragments and values;
- `run/install/scheduler/`: non-secret native scheduler definitions.

There is no separate completion record. The transaction reaches `committed` only
after the target manifest is durably published and verified. A crash between those
steps is reconciled from the manifest digest.

The first slice owns only the Unix shell-profile marker block, the Windows user
values `LLM_WIKI_ROOT` and `LLM_WIKI_STATE_ROOT`, Windows scheduled tasks, macOS
per-user LaunchAgents, Linux per-user systemd units, and an explicitly selected cron
block. Existing agent-integration configuration mergers and opt-in Git push
protection keep their current component-level contracts. Code upgrade, full release
inventory, restored-vault publication, and enrollment of those remaining external
mutations are later slices.

## State And Recovery

`install-transaction/v1` uses the operation states `prepared`, `mutating`,
`publishing`, `committed`, `reverting`, `reverted`, and `quarantined`. Each resource
uses `pending`, `mutating`, `verified`, `reverting`, or `reverted`.

- A durable `prepared` transaction and all required preimages exist before the first
  external mutation.
- The next identical request resumes a nonterminal transaction by inspecting the
  real resource state. A different request must first restore the old transaction.
- Rollback runs in reverse order and restores a resource only when its current value
  is the expected installed value. Concurrent user edits are never overwritten.
- Unknown existing resources, malformed state, digest mismatch, or ambiguous
  historical ownership fail closed. External drift moves the transaction to
  `quarantined`; there is no force flag.
- An active manifest, nonterminal transaction, quarantine, or unreadable install
  state blocks an offline `run/` deletion snapshot.

Canonical JSON is bounded to 1 MiB per record. A transaction owns no more than 32
resources. Each persisted fragment or scheduler definition is bounded to 2 MiB and
all preimages together are bounded to 16 MiB. Files are written with atomic replace,
file fsync, and parent-directory fsync on the supported local-filesystem boundary.

## Preimage And Ownership Boundary

The control plane does not copy whole shell profiles or crontabs because they can
contain unrelated credentials. It stores only the exact LLM-Wiki-owned fragment or
logical value, its SHA-256 digest and size, plus the metadata needed by the resource
adapter. Existing profile mode, UID, and GID are preserved.

An existing product-named task, unit, plist, environment value, or marker block is
adopted only when it is an exact recognized legacy LLM-Wiki definition. Otherwise the
installer reports a conflict without mutation. Preimages and scheduler definitions
must not contain provider credentials, backup passwords, or unrelated user content.

## Scheduler Selection

Windows uses Task Scheduler. macOS uses per-user LaunchAgents. Linux uses the user
systemd manager and stores units under the effective `$XDG_CONFIG_HOME/systemd/user`
or `~/.config/systemd/user`. Cron is selected only through an explicit degraded
installer option; it is never an automatic fallback.

LaunchAgents and the default user-systemd path are login-scoped. The product does not
claim wake-from-sleep or logged-out execution. Linux calendar timers use persistent
catch-up after the user manager starts. Native registration is successful only after
the installed definition and loaded/enabled state are both verified.

## Consequences

- Persistent profile and Windows user-environment writes move after the mandatory
  production smoke gate.
- Windows no longer unregisters an unknown task before ownership is proven.
- Installer interruption is resumable and an install failure can restore earlier
  verified mutations without guessing.
- Dirty local checkouts are recorded as unverified source state and are not described
  as immutable releases.
- The first slice advances but does not close the full upgrade, immutable-release,
  and whole-install uninstall findings.

## Source / Evidence

- Explicit user approval in the 2026-08-16 OpenCode session.
- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]].
- Microsoft `Export-ScheduledTask`: https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/export-scheduledtask
- Microsoft `Register-ScheduledTask`: https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask
- Apple launchd jobs: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- systemd timers: https://man7.org/linux/man-pages/man5/systemd.timer.5.html
- systemd unit paths: https://man7.org/linux/man-pages/man5/systemd.unit.5.html
- XDG Base Directory Specification: https://specifications.freedesktop.org/basedir-spec/latest/

## Related

- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/integration-config-backup-retention-decision]]
- [[knowledge/notes/reliability-v3-runtime-adoption-implementation-decision]]
