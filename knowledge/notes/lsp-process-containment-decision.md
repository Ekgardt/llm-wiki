---
type: decision
title: "Platform-Qualified LSP Process Containment"
description: "Windows Job Objects own assigned LSP trees, while POSIX process groups cover only pinned Pyright descendants that remain in the group."
date: 2026-07-24
confidence: high
source_authority: user
status: active
---
# Platform-Qualified LSP Process Containment

One-sentence summary: Windows Job Objects own assigned LSP trees, while POSIX process groups cover only pinned Pyright descendants that remain in the group.

## Decision

Date: 2026-07-24.

LLM Wiki uses a Windows Job Object as the ownership boundary for the assigned
Pyright server and its child processes. On Linux and macOS, LLM Wiki uses a POSIX
process group created by `start_new_session=True`, then sends bounded `SIGTERM` and
`SIGKILL` signals to that group.

The POSIX process group owns the pinned, qualified Pyright server and descendants
only while they remain in that group. A hostile descendant can call `setsid()` and
create a new session and process group. Portable containment of that escape is
unsupported. POSIX LSP execution therefore remains limited to pinned Pyright in
operator-trusted local repositories; it is not a hostile-workload sandbox.

No `/proc` or `ps` ancestry scan will be added or described as ownership. Such an
ancestry scan is race-prone, exposed to PID reuse, unavailable as one equivalent
portable macOS contract, and cannot prevent a process from changing groups. The
bounded Linux `/proc` observation already used to distinguish live members of the
owned group from zombies is not an ancestry scan and does not expand containment.

## What Is True Today (2026-07-24)

This section records what is true today: Windows has assigned-tree ownership through
the Job Object, and POSIX has qualified process-group cleanup for the pinned Pyright
profile. The negative fixture proves that a coordinated `setsid()` descendant is
outside the POSIX boundary and then has its parent kill and reap it explicitly.

## Future Direction

This section separates what we want it to become from current behavior. A delegated
cgroup v2 boundary is a future, optional Linux-only hardening candidate. It requires
a separate approved design, capability detection, safe delegation, and fallback
semantics. It is not current behavior, does not solve macOS, and introduces no
persistent daemon or new runtime path.

## Consequences

- Tests and documentation say "owned process group" on POSIX, not "whole process
  tree" without qualification.
- The pinned Pyright profile and trusted-repository requirement are part of the
  containment claim.
- Known `setsid()` escape is tested as unsupported and is never counted as successful
  containment.
- Windows directory durability barriers for owner evidence and live leases fail
  closed; a false flush result cannot publish terminal success.
- POSIX evidence retries repeat the owner-directory `fsync` after a prior failed
  publication barrier, even when canonical evidence bytes already match.

## Source / Evidence

- Explicit user approval and audit remediation direction, 2026-07-24.
- POSIX `setsid()`, Issue 8: https://pubs.opengroup.org/onlinepubs/9799919799/functions/setsid.html
- Windows Job Objects: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- Python subprocess session creation: https://docs.python.org/3/library/subprocess.html
- Linux cgroup v2: https://docs.kernel.org/admin-guide/cgroup-v2.html
- `docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md`
- `tests/test_lsp_process.py` and `tests/test_lsp_process_tree.py`

## Related

- [[read-only-lsp-navigation-engine-decision]]
- [[lsp-live-lease-decision]]
