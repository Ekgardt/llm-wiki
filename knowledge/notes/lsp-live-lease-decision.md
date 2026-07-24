---
type: decision
title: "LSP Live Lease"
description: "Live LSP lifecycle ownership is represented by one bounded mutable lease anchored inside the immutable owner directory."
date: 2026-07-23
confidence: high
source_authority: user
status: active
---
# LSP Live Lease

One-sentence summary: Live LSP lifecycle ownership is represented by one bounded mutable lease anchored inside the immutable owner directory.

## Decision

Date: 2026-07-23.

Each live LSP owner maintains `run/lsp/<owner-nonce>/lease.json` as a bounded
mutable live lease. It is distinct from immutable, create-only `owner.json` and
`failure.json`. The owner refreshes the lease every 10 seconds and gives each
refresh a 30 seconds expiry.

The lease exists only while lifecycle ownership is live. Controlled success or
terminal failure stops and joins the heartbeat before removing it. Abrupt owner
death leaves the lease to expire so doctor can later distinguish stale ownership.
Failure retains immutable `owner.json` and `failure.json`; success removes the
owner scratch after the process tree and lifecycle threads have stopped.

The canonical lease contains only a schema version, manager PID, server PID,
owner nonce, current generation nonce, heartbeat UTC, expiry UTC, and live state.
It never contains a repository path, command argument, environment value, stderr,
or credential. Updates are atomic, owner-only, and anchored and fenced to the
retained owner-directory handle. POSIX uses handle-relative temp creation, file
flush, replace, and directory flush. Windows uses the smallest equivalent safe
handle-relative filesystem extension.

## Rationale

Immutable evidence proves which lifecycle was created and why it failed, but it
cannot safely represent changing liveness. A separate short-lived lease makes live,
stale, successful, and failed ownership unambiguous without weakening create-only
evidence or exposing process inputs. The 10/30 cadence tolerates short scheduling
delays while bounding stale ownership recognition.

## Consequences

- Task 13 doctor may treat an unexpired lease as live and an expired lease as stale.
- Lease replacement must never follow a lexical path outside the retained owner
  directory or replace immutable evidence.
- Normal cleanup does not claim coverage for signals, `os._exit()`, or fatal
  interpreter termination; expiry handles those abandoned leases.
- The existing runtime root and deletion contract do not change.

## Source / Evidence

- Explicit user authorization and delegated engineering selection, 2026-07-23.
- Windows Job Objects: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- AssignProcessToJobObject: https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
- Windows process creation flags: https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
- ResumeThread: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread
- Python subprocess: https://docs.python.org/3/library/subprocess.html
- POSIX setsid: https://man7.org/linux/man-pages/man2/setsid.2.html
- POSIX killpg: https://man7.org/linux/man-pages/man2/killpg.2.html
- Python atexit: https://docs.python.org/3/library/atexit.html

## Related

- [[lsp-process-containment-decision]]
- [[read-only-lsp-navigation-engine-decision]]
