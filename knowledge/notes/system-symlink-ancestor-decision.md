---
type: decision
status: accepted
date: 2026-08-18
---

# System-owned symlink ancestors

One-sentence summary: A bounded read accepts a symlinked ancestor only when
root owns both the link and the directory holding it, instead of refusing every
symlink in the path.

## What changed

`read_stable_bytes` in `scripts/bounded_io.py` walked a path's ancestors and
raised `PermissionError` for any symlink among them. It now rejects an ancestor
symlink unless `_system_symlink` proves an unprivileged user could not have
created or replaced it: the link belongs to root, and so does the directory
containing it, which must not be group- or other-writable. Symlink mode bits
are meaningless on Linux, so the containing directory carries the decision.

Nothing else moved. The final component must still be a regular non-symlink
file, the open still uses `O_NOFOLLOW`, and the device/inode/size/mtime
identity is still verified before the read, after the read, and against the
path itself.

## Why

macOS ships `/var` as a root-owned symlink to `/private/var`, and every
temporary directory lives under it. The old rule therefore refused to read any
temporary file on macOS: two benchmark tests failed with "parent must be a
regular directory" on every macOS runner, and the same refusal would meet any
user of the installed system there.

Current traversal-resistant APIs draw the line at escape, not at symlinks.
`openat2(RESOLVE_BENEATH)` follows links that stay inside the starting
directory and reserves `RESOLVE_NO_SYMLINKS` for targeted use — its manual page
asks applications to make that strictness configurable. FreeBSD's
`O_RESOLVE_BENEATH` matches Linux. Go 1.24's `os.Root` states the threat model
in the same terms this decision uses: it defends against constructs an ordinary
user can create, and not against ones that require root.

Research recorded in `docs/research/2026-08-18-traversal-resistant-reads.md`.

## What this gives up

An administrator — or anything running as root — can now redirect a bounded
read by placing a symlink in a root-owned directory. That was already true of
every other file this process reads, and a root-controlled machine is outside
the threat model by construction. What remains defended is the case that
matters: a symlink planted by an unprivileged user in a directory they can
write, including anything under a world-writable or sticky directory such as
`/tmp`.

The rule is a property of the ancestor, not of a configured root. A caller that
needs the stricter "no symlink at all" posture has no switch for it today; if
one is ever needed, it belongs as an explicit parameter rather than as the
default.

## Alternatives considered

* **Resolve the path first.** One line, and it removes the check entirely: a
  resolved path has no symlink components by construction, so every ancestor
  would pass regardless of who owns it.
* **Normalize in each caller.** `benchmark/run_retrieval_v2.py` creates the
  temporary directory behind the failing reads, so resolving it there would fix
  those two tests without touching the boundary. It leaves the reader broken
  for every other macOS caller, and that file carries 58 complexity-gate
  findings — the largest function measures 124 — so the edit would have pulled
  a multi-day refactor of the measurement authority into a portability fix.
* **A trusted-root parameter, as `os.Root` has.** The strongest option and the
  right long-term shape, but it changes every call site and needs a root to
  pass; the ownership rule solves the platform problem without that churn.

## Source / Evidence

- `scripts/bounded_io.py` — `_system_symlink`, `_acceptable_ancestor`.
- `tests/test_bounded_io.py` — a user-owned symlink ancestor is still refused; a
  system-owned one is accepted; the ownership rule requires both the link and
  its directory.
- `docs/research/2026-08-18-traversal-resistant-reads.md` — the sources behind
  the boundary, including `openat2(2)` and the Go `os.Root` threat model.
- macOS CI evidence: run `32139596738`, job `timing::macos_full::py3.14`.

## Related

- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]]
- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/reliable-memory-stage-2]]
